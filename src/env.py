from posixpath import join
from subprocess import run, PIPE, Popen, CalledProcessError, check_output
from configparser import ConfigParser
from os.path import (exists, split)
from os import chmod, remove
from shutil import rmtree, copytree, copyfile
from pathlib import Path
import python_freeipa as pipa
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
import pwd
from decouple import config, UndefinedValueError

from SCAutolib import env_logger
from SCAutolib.src import utils, exceptions
from SCAutolib.src import *


def create_cnf(user: str, conf_dir=None, ca_dir=None):
    """
    Create configuration files for OpenSSL to generate certificates and requests.
    """
    if user == "ca":
        if ca_dir is None:
            env_logger.warn("Parameter ca_dir is None. Try to read from config file")
            ca_dir = read_config("ca_dir")
            if ca_dir is None:
                env_logger.error("No value for ca_dir in config file")
                raise exceptions.UnspecifiedParameter("ca_dir", "CA directory is not provided")

        if conf_dir is None:
            conf_dir = join(ca_dir, "conf")
        ca_cnf = f"""
[ ca ]
default_ca = CA_default

[ CA_default ]
dir              = {ca_dir}
database         = $dir/index.txt
new_certs_dir    = $dir/newcerts

certificate      = $dir/rootCA.crt
serial           = $dir/serial
private_key      = $dir/rootCA.key
RANDFILE         = $dir/rand

default_days     = 365
default_crl_hours = 1
default_md       = sha256

policy           = policy_any
email_in_dn      = no

name_opt         = ca_default
cert_opt         = ca_default
copy_extensions  = copy

[ usr_cert ]
authorityKeyIdentifier = keyid, issuer

[ v3_ca ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer:always
basicConstraints       = CA:true
keyUsage               = critical, digitalSignature, cRLSign, keyCertSign

[ policy_any ]
organizationName       = supplied
organizationalUnitName = supplied
commonName             = supplied
emailAddress           = optional

[ req ]
distinguished_name = req_distinguished_name
prompt             = no

[ req_distinguished_name ]
O  = Example
OU = Example Test
CN = Example Test CA"""

        with open(f"{conf_dir}/ca.cnf", "w") as f:
            f.write(ca_cnf)
            env_logger.debug(
                f"Configuration file for local CA is created {conf_dir}/ca.cnf")
        return

    user_cnf = f"""
[ req ]
distinguished_name = req_distinguished_name
prompt = no

[ req_distinguished_name ]
O = Example
OU = Example Test
CN = {user}

[ req_exts ]
basicConstraints = CA:FALSE
nsCertType = client, email
nsComment = "{user}"
subjectKeyIdentifier = hash
keyUsage = critical, nonRepudiation, digitalSignature
extendedKeyUsage = clientAuth, emailProtection, msSmartcardLogin
subjectAltName = otherName:msUPN;UTF8:{user}@EXAMPLE.COM, email:{user}@example.com
"""
    if conf_dir is None:
        raise exceptions.UnspecifiedParameter("conf_dir", "Directory with configurations is not provided")
    with open(f"{conf_dir}/req_{user}.cnf", "w") as f:
        f.write(user_cnf)
        env_logger.debug(f"Configuration file for CSR for user {user} is created "
                         f"{conf_dir}/req_{user}.cnf")


def create_sssd_config():
    """
    Update the content of the sssd.conf file. If file exists, it would be store
    to the backup folder and content in would be edited for testing purposes.
    If file doesn't exist, it would be created and filled with default options.
    """
    cnf = ConfigParser(allow_no_value=True)
    cnf.optionxform = str  # Needed for correct parsing of uppercase words
    default = {
        "sssd": {"#<[sssd]>": None,
                 "debug_level": "9",
                 "services": "nss, pam",
                 "domains": "shadowutils"},
        "nss": {"#<[nss]>": None,
                "debug_level": "9"},
        "pam": {"#<[pam]>": None,
                "debug_level": "9",
                "pam_cert_auth": "True"},
        "domain/shadowutils": {"#<[domain/shadowutils]>": None,
                               "debug_level": "9",
                               "id_provider": "files"},
    }

    cnf.read_dict(default)

    sssd_conf = "/etc/sssd/sssd.conf"
    if exists(sssd_conf):
        bakcup_dir = utils.backup_(sssd_conf, name="sssd-original.conf")
        add_restore("file", sssd_conf, bakcup_dir)

    with open(sssd_conf, "w") as f:
        cnf.write(f)
        env_logger.debug("Configuration file for SSSD is updated "
                         "in  /etc/sssd/sssd.conf")
    chmod(sssd_conf, 0o600)


def create_softhsm2_config(card_dir: str):
    """
    Create SoftHSM2 configuration file in conf_dir. Same directory has to be used
    in setup-ca function, otherwise configuration file wouldn't be found causing
    the error. conf_dir expected to be in work_dir.
    """
    conf_dir = f"{card_dir}/conf"

    with open(f"{conf_dir}/softhsm2.conf", "w") as f:
        f.write(f"directories.tokendir = {card_dir}/tokens/\n"
                f"slots.removable = true\n"
                f"objectstore.backend = file\n"
                f"log.level = INFO\n")
        env_logger.debug(f"Configuration file for SoftHSM2 is created "
                         f"in {conf_dir}/softhsm2.conf.")


def create_virt_card_service(username: str, card_dir: str):
    """
    Create systemd service for for virtual smart card (virt_cacard.service).
    """
    path = f"/etc/systemd/system/virt_cacard_{username}.service"
    conf_dir = f"{card_dir}/conf"
    default = {
        "Unit": {
            "Description": f"virtual card for {username}",
            "Requires": "pcscd.service"},
        "Service": {
            "Environment": f'SOFTHSM2_CONF="{conf_dir}/softhsm2.conf"',
            "WorkingDirectory": card_dir,
            "ExecStart": "/usr/bin/virt_cacard >> /var/log/virt_cacard.debug 2>&1",
            "KillMode": "process"
        },
        "Install": {"WantedBy": "multi-user.target"}
    }
    cnf = ConfigParser()
    cnf.optionxform = str

    if exists(path):
        name = split(path)[1].split(".", 1)
        name = name[0] + "-original." + name[1]
        backup_dir = utils.backup_(path, name)
        add_restore("file", path, backup_dir)

    with open(path, "w") as f:
        cnf.read_dict(default)
        cnf.write(f)
    env_logger.debug(f"Service file {path} for user '{username}' "
                     "is created.")


def read_env(item: str, *args, **kwargs):
    return config(item, *args, **kwargs)


def read_config(*items):
    """
    Read data from the configuration file and return require items or full
    content.

    Args:
        items: list of items to extracrt from the configuration file.
               If None, full contant would be returned

    Returns:
        list with required items
    """
    try:
        with open(read_env("CONF"), "r") as file:
            config_data = yaml.load(file, Loader=yaml.FullLoader)
            assert config_data, "Data are not loaded correctly."
    except FileNotFoundError as e:
        env_logger.error(".env file is not present. Try to rerun command"
                         "with --conf </path/to/conf.yaml> parameter")
        raise e

    if items is None:
        return config_data

    return_list = []
    for item in items:
        parts = item.split(".")
        value = config_data
        for part in parts:
            if value is None:
                env_logger.warn(
                    f"Key {part} not present in the configuration file. Skip.")
                return None

            value = value.get(part)
            if part == parts[-1]:
                return_list.append(value)

    return return_list if len(items) > 1 else return_list[0]


def setup_ca_(env_file: str):
    ca_dir = read_env("CA_DIR")
    env_logger.debug("Start setup of local CA")

    try:
        check_output(["bash", SETUP_CA, "--dir", ca_dir, "--env", env_file],
                     encoding="utf-8")
        env_logger.debug("Setup of local CA is completed")
    except CalledProcessError:
        env_logger.error("Error while setting up local CA")
        exit(1)


def setup_virt_card_(user: dict):
    """
    Call setup script fot virtual smart card

    Args:
        user: dictionary with user information
    """

    username, card_dir, passwd = user["name"], user["card_dir"], user["passwd"]
    cmd = ["bash", SETUP_VSC, "--dir", card_dir, "--username", username]
    if user["local"]:
        try:
            pwd.getpwnam(username)
        except KeyError:
            check_output(["useradd", username, "-m", ], encoding="utf-8")
            env_logger.debug(f"Local user {username} is added to the system "
                             f"with a password {passwd}")
        finally:
            with Popen(['passwd', username, '--stdin'], stdin=PIPE,
                       stderr=PIPE, encoding="utf-8") as proc:
                proc.communicate(passwd)
            env_logger.debug(f"Password for user {username} is updated to {passwd}")
        create_cnf(username, conf_dir=join(card_dir, "conf"))
        cnf = ConfigParser()
        cnf.optionxform = str
        with open("/etc/sssd/sssd.conf", "r") as f:
            cnf.read_file(f)

        if f"certmap/shadowutils/{username}" not in cnf.sections():
            cnf.add_section(f"certmap/shadowutils/{username}")

        cnf.set(f"certmap/shadowutils/{username}", "matchrule",
                f"<SUBJECT>.*CN={username}.*")
        with open("/etc/sssd/sssd.conf", "w") as f:
            cnf.write(f)
        env_logger.debug("Match rule for local user is added to /etc/sssd/sssd.conf")
    try:
        if user["cert"]:
            cmd += ["--cert", user["cert"]]
        else:
            raise KeyError
        if user["key"]:
            cmd += ["--key", user["key"]]
        else:
            raise KeyError()
    except KeyError:
        ca_dir = read_env("CA_DIR")
        cmd += ["--ca", ca_dir]
        env_logger.debug(f"Key or certificate for user {username} "
                         f"is not present. New pair of key and cert will "
                         f"be generated by local CA from {ca_dir}")

    env_logger.debug(f"Start setup of virtual smart card for user {username} "
                     f"in {card_dir}")
    try:
        check_output(cmd, encoding="utf-8")
        env_logger.debug(f"Setup of virtual smart card for user {username} "
                         f"is completed")
    except CalledProcessError:
        env_logger.error("Error while setting up virtual smart card")
        raise


def check_semodule():
    result = check_output(["semodule", "-l"], stderr=PIPE, encoding="utf-8")
    if "virtcacard" not in result.stdout:
        env_logger.debug(
            "SELinux module for virtual smart cards is not present in the "
            "system. Installing...")
        conf_dir = join(read_env("CA_DIR"), 'conf')
        module = """
(allow pcscd_t node_t(tcp_socket(node_bind)))

; allow p11_child to read softhsm cache - not present in RHEL by default
(allow sssd_t named_cache_t(dir(read search)))"""
        with open(f"{conf_dir}/virtcacard.cil", "w") as f:
            f.write(module)
        try:
            check_output(["semodule", "-i", f"{conf_dir}/virtcacard.cil"],
                         encoding="utf-8")
            env_logger.debug(
                "SELinux module for virtual smart cards is installed")
        except CalledProcessError:
            env_logger.error("Error while installing SELinux module "
                             "for virt_cacard")
            raise

        try:
            check_output(["systemctl", "restart", "pcscd"], encoding="utf-8")
            env_logger.debug("pcscd service is restarted")
        except CalledProcessError:
            env_logger.error("Error while resturting the pcscd service")
            raise


def prepare_dir(dir_path: str, conf=True):
    Path(dir_path).mkdir(parents=True, exist_ok=True)
    env_logger.debug(f"Directory {dir_path} is created")
    if conf:
        Path(join(dir_path, "conf")).mkdir(parents=True, exist_ok=True)
        env_logger.debug(f"Directory {join(dir_path, 'conf')} is created")


def prep_tmp_dirs():
    """
    Prepair directory structure for test environment. All paths are taken from
    previously loaded env file.
    """
    paths = [read_env(path, cast=str) for path in ("CA_DIR", "TMP", "BACKUP")] + \
            [join(read_env("CA_DIR"), "conf")]
    for path in paths:
        prepare_dir(path, conf=False)


def install_ipa_client_(ip: str, passwd: str):
    env_logger.debug(f"Start installation of IPA client")
    args = ["bash", INSTALL_IPA_CLIENT, "--ip", ip, "--root", passwd]
    env_logger.debug(f"Aruments for script: {args}")
    try:
        check_output(args, encoding="utf-8")
        env_logger.debug("IPA client is configured on the system. "
                         "Don't forget to add IPA user by add-ipa-user command :)")
    except CalledProcessError:
        env_logger.error("Error while installing IPA client on local host")
        raise


def add_ipa_user_(user: dict):
    username, user_dir = user["name"], user["card_dir"]
    env_logger.debug(f"Adding user {username} to IPA server")
    ipa_admin_passwd, ipa_hostname = read_config("ipa_server_admin_passwd", "ipa_server_hostname")
    client = pipa.ClientMeta(ipa_hostname, verify_ssl=False)
    client.login("admin", ipa_admin_passwd)
    try:
        client.user_add(username, username, username, username)
    except pipa.exceptions.DuplicateEntry:
        env_logger.warn(f"User {username} already exists in the IPA server "
                        f"{ipa_hostname}")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    prepare_dir(user_dir)

    with open(f"{user_dir}/private.key", "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()))
    try:
        cmd = ["openssl", "req", "-new", "-days", "365",
               "-nodes", "-key", f"{user_dir}/private.key", "-out",
               f"{user_dir}/cert.csr", "-subj", f"/CN={username}"]
        check_output(cmd, encoding="utf-8")
    except CalledProcessError:
        env_logger.error(f"Error while generating CSR for user {username}")
        raise
    try:
        cmd = ["ipa", "cert-request", f"{user_dir}/cert.csr", "--principal",
               username, "--certificate-out", f"{user_dir}/cert.pem"]
        check_output(cmd, encoding="utf-8")
    except CalledProcessError:
        env_logger.error(f"Error while requesting the certificate for user "
                         f"{username} from IPA server")
        raise

    env_logger.debug(f"User {username} is updated on IPA server. "
                     f"Cert and key stored into {user_dir}")


def setup_ipa_server_():
    check_output(["bash", SETUP_IPA_SERVER], encoding="utf-8")


def general_setup(install_missing: bool = False):
    args = ['bash', GENERAL_SETUP]
    if install_missing:
        args += ["--install-missing"]
    if read_env("READY", cast=int, default=0) != 1:
        check_semodule()
        try:
            check_output(args, encoding="utf-8")
        except CalledProcessError:
            env_logger.error("Script for general setup is failed")
            raise


def create_sc(sc_user: dict):
    name, card_dir = sc_user["name"], sc_user["card_dir"]
    prepare_dir(card_dir)
    create_softhsm2_config(card_dir)
    env_logger.debug("SoftHSM2 configuration file is created in the "
                     f"{card_dir}/conf/softhsm2.conf")
    create_virt_card_service(name, card_dir)
    env_logger.debug(f"Start setup of virtual smart cards for local user {name}")
    setup_virt_card_(sc_user)


def check_config(conf: str) -> bool:
    """Check if all required fields are present in the config file. Warn user if
    some fields are missing.
    Args:
        conf: path to configuration file in YAML format
    Return:
        True if config file contain everyting what is needed. Otherwise False.
    """
    with open(conf, "r") as file:
        config_data = yaml.load(file, Loader=yaml.FullLoader)
        assert config_data, "Data are not loaded correctly."
    result = True
    fields = ("root_passwd", "ca_dir", "ipa_server_root", "ipa_server_ip",
              "ipa_server_hostname", "ipa_client_hostname", "ipa_domain",
              "ipa_realm", "ipa_server_admin_passwd", "local_user", "ipa_user")
    config_fields = config_data.keys()
    for f in fields:
        if f not in config_fields:
            env_logger.warning(f"Field {f} is not present in the config.")
            result = False
    if result:
        env_logger.debug("Configuration file is OK.")
    return result


def add_restore(type_: str, src: str, backup: str = None):
    """Add new item to be restored in the cleanup phase.

    Args:
        type_: type of item. Cane be one of user, file or dir. If type is not
               matches any of mentioned types, warning is written, but item
               is added.
        src: for file and dir should be an original path. For type == user
             should be username
        backup: applicable only for file and dir type. Path where original
                source was placed.
    """
    with open(read_env("CONF"), "r") as f:
        data = yaml.load(f, Loader=yaml.FullLoader)

    if type_ not in ("user", "file", "dir"):
        env_logger.warning(f"Type {type_} is not know, so this item can't be "
                           f"correctly restored")
    data["restore"].append({"type": type_, "src": src, "backup_dir": backup})

    with open(read_env("CONF"), "w") as f:
        yaml.dump(data, f)


def cleanup_(restore_items: dict):
    for item in restore_items:
        type_ = item['type']
        src = item['src'] if type_ != "user" else item["username"]
        backup_dir = item["backup_dir"] if "backup_dir" in item.keys() else None

        if type_ == "file":
            if backup_dir:
                copyfile(backup_dir, src)
                env_logger.debug(f"File {src} is restored form {backup_dir}")
            else:
                remove(src)
                env_logger.debug(f"File {src} is deleted")
        elif type_ == "dir":
            rmtree(src, ignore_errors=True)
            env_logger.debug(f"Directory {src} is deleted")
            if backup_dir:
                copytree(backup_dir, src)
                env_logger.debug(f"Directory {src} is restored form {backup_dir}")

        elif type_ == "user":
            username = item["username"]
            check_output(["userdel", username, "-r"], encoding="utf-8")
            env_logger.debug(f"User {username} is delete with it home directory")
        else:
            env_logger.warning(f"Skip item with unknow type '{type_}'")
