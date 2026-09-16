"""容器入口：必要时修复数据目录属主并降权，再把 PID 1 交给业务进程。

Kubernetes（Zeabur 即基于此）挂载持久卷时，挂载点属主由平台决定，通常是 root，
会盖掉镜像里预设好的属主。业务服务以非 root 的 monitor 运行，直接启动就会在写入
数据目录时失败。本脚本在拿得到 root 时修复属主后降权；拿不到 root 时先做一次
体检，把晦涩的 PermissionError 换成可操作的提示。

Docker Compose 同样会先以 root 运行入口，再降权执行服务；因此挂载命名卷和托管平台
持久卷使用同一套权限处理逻辑。
"""
import os
import sys

RUNTIME_UID = 10001
RUNTIME_GID = 10001
DEFAULT_DATA_DIR = "/data"


def fail(message):
    print(message, file=sys.stderr, flush=True)
    raise SystemExit(1)


def take_ownership(data_dir):
    """把数据目录及其内容的属主改为运行用户，仅在以 root 启动时调用。"""
    try:
        os.makedirs(data_dir, mode=0o700, exist_ok=True)
    except OSError as exc:
        fail(f"无法创建数据目录 {data_dir}：{exc}")
    paths = [data_dir]
    for parent, directories, files in os.walk(data_dir):
        paths.extend(os.path.join(parent, name) for name in (*directories, *files))
    for path in paths:
        try:
            # 不跟随符号链接，避免卷内的链接把 chown 引到数据目录之外。
            os.chown(path, RUNTIME_UID, RUNTIME_GID, follow_symlinks=False)
        except OSError as exc:
            fail(f"无法修改 {path} 的属主：{exc}")


def drop_privileges():
    """降权到运行用户。setgroups 必须先于 setgid，否则会残留 root 的附加组。"""
    os.setgroups([])
    os.setgid(RUNTIME_GID)
    os.setuid(RUNTIME_UID)


def ensure_writable(data_dir):
    """以非 root 启动时确认数据目录可用，不可用则给出可操作的诊断。"""
    if not os.path.isdir(data_dir):
        fail(f"数据目录 {data_dir} 不存在，请确认持久卷已挂载到该路径")
    if os.access(data_dir, os.W_OK | os.X_OK):
        return
    owner = os.stat(data_dir)
    fail(f"数据目录 {data_dir} 对当前用户（uid={os.getuid()}）不可写，"
         f"该目录属主为 uid={owner.st_uid}、gid={owner.st_gid}。\n"
         "持久卷挂载后属主通常是 root，需要让容器以 root 启动，"
         "由本入口脚本修复属主后再降权运行。")


def main():
    command = sys.argv[1:]
    if not command:
        fail("入口脚本需要一个待执行的命令，例如：entrypoint.py python -u server.py")
    data_dir = os.environ.get("DATA_DIR", DEFAULT_DATA_DIR)
    if os.geteuid() == 0:
        take_ownership(data_dir)
        drop_privileges()
    else:
        ensure_writable(data_dir)
    # 用 execvp 顶替当前进程，业务进程才能接管 PID 1 并直接收到容器停止时的 SIGTERM。
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
