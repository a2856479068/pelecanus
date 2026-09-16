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
# 属主修复以 root 执行，误配置的 DATA_DIR 会把系统目录改掉属主，等于给业务进程留下提权口子。
SYSTEM_DIRS = frozenset("""
/ /app /bin /boot /dev /etc /home /lib /lib64 /media /mnt /opt /proc /root /run /sbin /srv /sys /usr /var
""".split())
# 路径黑名单靠精确匹配，挡不住它没列全的目录；数据目录正常只有数据库和锁文件，
# 条目数远超预期就说明 DATA_DIR 指错了位置，这里在动手改属主之前兜一道。
MAX_OWNED_PATHS = 1000


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
    if len(paths) > MAX_OWNED_PATHS:
        fail(f"数据目录 {data_dir} 下有 {len(paths)} 个条目，超过预期上限 {MAX_OWNED_PATHS}，"
             "疑似 DATA_DIR 指向了错误的位置，已在修改属主前中止")
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


def resolve_data_dir():
    """解析数据目录，挡掉会让 root 递归 chown 破坏系统的取值。

    DATA_DIR 是对外公开的配置项，误填成系统路径会把属主改成业务用户，等于给服务进程
    留下改写 /etc/passwd 之类的机会。按顶层目录判断而不是逐个精确匹配，否则
    /etc/nginx、/var/lib/docker 这些子路径会从清单的缝里漏过去。
    """
    data_dir = os.path.realpath(os.environ.get("DATA_DIR", DEFAULT_DATA_DIR))
    top_level = "/" + data_dir.strip("/").split("/")[0]
    if top_level in SYSTEM_DIRS:
        fail(f"DATA_DIR 不能落在系统目录下（解析为 {data_dir}），请改用 /data 这样的专用目录")
    return data_dir


def main():
    command = sys.argv[1:]
    if not command:
        fail("入口脚本需要一个待执行的命令，例如：entrypoint.py python -u server.py")
    data_dir = resolve_data_dir()
    if os.geteuid() == 0:
        take_ownership(data_dir)
        drop_privileges()
    else:
        ensure_writable(data_dir)
    # 用 execvp 顶替当前进程，让业务进程直接成为 PID 1。PID 1 只收得到自己注册过
    # handler 的信号，容器停止时的 SIGTERM 由 server.py 接管，不然只能等着被 SIGKILL。
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
