"""
Copy-paste recipes for turbossh. Edit credentials and call the one you need.
Run `python examples/examples.py` to print this list.
"""

from turbossh import (SSHHandler, SSHConfig, FTPHandler, FTPConfig, SSHPool,
                         Secret, CredentialStore, prompt_password)


# 1. Password login (raise-on-error — good for test automation / scripts)
def password_login():
    cfg = SSHConfig(host="192.168.1.10", username="root", password="secret")
    with SSHHandler(cfg) as ssh:
        print(ssh.run("uname -a").stdout)
        ssh.run("systemctl restart nginx", check=True)   # raises on failure


# 2. RDP / Windows host via OpenSSH using a DOMAIN account (CORP\myuser).
#    Pass domain + username SEPARATELY so the literal "\n"-style escape trap is
#    avoided, and pull the password from the OS credential vault (never hard-coded).
def domain_windows_host():
    store = CredentialStore(service="my_test_lab")
    password = store.get("CORP\\myuser") or prompt_password("CORP\\myuser password: ")
    cfg = SSHConfig(
        host="10.20.30.40", domain="CORP", username="myuser",
        password=password,           # a Secret -> never logged/printed
        remote_os="windows",         # skip OS auto-probe for speed
        fast_auth=True,              # skip key probing -> faster login
    )
    with SSHHandler(cfg) as ssh:
        print(ssh.run("whoami").stdout)              # CORP\myuser
        print(ssh.run("powershell Get-Process").stdout)
        ssh.push("report.xlsx", "C:/Users/myuser/Desktop/report.xlsx")


# 3. Store a domain password once into the OS vault (then reuse via #2)
def store_password_once():
    CredentialStore("my_test_lab").set("CORP\\myuser",
                                        prompt_password("Password to store: "))


# 4. Passwordless / key-based login
def passwordless():
    with SSHHandler(SSHConfig(host="server.lab", username="ubuntu",
                              passwordless=True)) as ssh:
        print(ssh.run("hostname").stdout)


# 5. Jump host / bastion (ProxyJump)
def via_jump_host():
    bastion = SSHConfig(host="bastion.example.com", username="jump", passwordless=True)
    cfg = SSHConfig(host="10.0.0.5", username="root", password="secret",
                    jump_host=bastion)
    with SSHHandler(cfg) as ssh:
        print(ssh.run("hostname").stdout)


# 6. Full file operations + transfer stats
def file_ops():
    with SSHHandler(SSHConfig(host="10.0.0.5", username="root",
                              password="secret")) as ssh:
        ssh.makedirs("/tmp/deploy/cfg")
        ssh.write_text("/tmp/deploy/cfg/app.conf", "debug=true\n")
        print(ssh.read_text("/tmp/deploy/cfg/app.conf"))
        ssh.chmod("/tmp/deploy/cfg/app.conf", 0o600)

        result = ssh.push("./build", "/tmp/deploy/build", recursive=True)
        print(result)                       # <TransferResult ... 12.3MB ... 4.1MB/s>
        ssh.pull("/var/log", "./logs", recursive=True,
                 callback=lambda done, total: None)
        for dirpath, dirs, files in ssh.walk("/etc/nginx"):
            print(dirpath, files)


# 7. Interactive shell (send/expect)
def interactive_shell():
    with SSHHandler(SSHConfig(host="10.0.0.5", username="root",
                              password="secret")) as ssh:
        with ssh.open_shell() as sh:
            sh.send("cd /var/log")
            sh.send("ls")
            print(sh.read_until("$", timeout=5).output)


# 8. Many hosts in parallel (fleet ops / test labs)
def parallel_fleet():
    configs = [SSHConfig(host=h, username="root", password="secret")
               for h in ("10.0.0.1", "10.0.0.2", "10.0.0.3")]
    with SSHPool(configs, max_workers=8) as pool:
        for host, res in pool.run("uptime").items():
            print(host, "->", res.value.stdout.strip() if res else res.error)
        pool.pull("/var/log/syslog", "logs/{host}_syslog.txt")


# 9. Plain FTP / FTPS (separate protocol)
def ftp_transfer():
    with FTPHandler(FTPConfig(host="ftp.example.com", username="u",
                              password="p", use_tls=True)) as ftp:
        ftp.push("local.txt", "remote.txt")
        ftp.pull("remote.txt", "copy.txt")
        print(ftp.listdir("/"))


# 10. pytest fixture for a test-automation framework
def pytest_fixture_example():
    """
        import pytest
        from turbossh import SSHHandler, SSHConfig, CredentialStore

        @pytest.fixture(scope="session")
        def dut():
            store = CredentialStore("test_lab")
            cfg = SSHConfig(host="10.0.0.5", domain="CORP", username="myuser",
                            password=store.get("CORP\\myuser"),
                            max_retries=5, auto_reconnect=True, command_timeout=60)
            ssh = SSHHandler(cfg)
            ssh.connect()
            yield ssh
            ssh.disconnect()

        def test_service_up(dut):
            assert dut.run("systemctl is-active nginx").stdout.strip() == "active"
    """


if __name__ == "__main__":
    print(__doc__)
    for name, fn in sorted(globals().items()):
        if callable(fn) and not name.startswith("_") and fn.__module__ == "__main__":
            print(f"  - {name}: {(fn.__doc__ or '').strip().splitlines()[0] if fn.__doc__ else ''}")
