import os
import socket
import sys
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime

from flask import Flask, redirect, request, url_for
from html import escape
import sqlite3

app = Flask(__name__)


def app_dir():
    """可执行文件（或源码）所在目录，绝不使用系统临时目录。

    PyInstaller --onefile 运行时会把资源解包到 sys._MEIPASS 指向的临时目录，
    如果按 __file__ 或 cwd 推导路径，数据库就会写进临时目录——每次启动都是
    全新的空库，用户数据直接丢光。冻结状态下必须用 sys.executable 的定位。
    另外 --onefile 在 macOS 会同时产出一个 .app 包：双击它时 sys.executable 是
    包内的 Contents/MacOS/AI业务台，直接取 dirname 会把数据库写进包体里，
    既看不到 data 文件夹，下次重建包还会丢数据。所以识别出这层结构后往上
    跳到 .app 的同级目录（用户拖到桌面的那个位置）。
    """
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        marker = os.sep + "Contents" + os.sep + "MacOS" + os.sep
        idx = exe.rfind(marker)
        if idx != -1:
            # .../AI业务台.app/Contents/MacOS/AI业务台 -> .../（.app 的同级）
            return os.path.dirname(exe[:idx])
        return os.path.dirname(exe)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


DATA_DIR = os.path.join(app_dir(), "data")
DB_PATH = os.path.join(DATA_DIR, "mvp.db")
PORT = 5000
PORT_TRIES = 20  # 5000 被占就顺延到 5019；macOS 的 ControlCenter、Windows 的 Hyper-V 保留段都可能吃掉 5000
HOST = "127.0.0.1"

# MySQL 的 DATETIME 默认值在 SQLite 里写成带括号的表达式；
# 不要用裸的 CURRENT_TIMESTAMP——那是 UTC，页面时间会比实际早 8 小时。
TS_NOW = "datetime('now','localtime')"

ERROR_LOG_NAME = "AI业务台-错误.log"


def error_log_path():
    """错误日志优先落在桌面，其次落在程序同级，最后落在用户主目录。

    --noconsole 打包后启动失败是完全静默的：没有黑框、没有弹窗、双击没反应，
    用户无从判断是端口被占还是目录不可写。留一个人类可读的日志文件，
    让对方把文件发过来就能定位。三个候选位置逐个检查存在且可写，
    因为「桌面」在中文系统里可能不存在（用户改过路径 / 没有 Desktop 目录）。
    """
    candidates = [
        os.path.expanduser(os.path.join("~", "Desktop")),
        app_dir(),
        os.path.expanduser("~"),
    ]
    for base in candidates:
        if base and os.path.isdir(base) and os.access(base, os.W_OK):
            return os.path.join(base, ERROR_LOG_NAME)
    return None


def write_error_log(what, exc=None):
    """把启动失败原因追加写进日志；本函数自身绝不抛异常。"""
    path = error_log_path()
    body = [
        f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {what}",
        f"  程序路径：{sys.executable}",
        f"  冻结模式：{getattr(sys, 'frozen', False)}",
        f"  数据库路径：{DB_PATH}",
    ]
    if exc is not None:
        body.append(f"  异常类型：{type(exc).__name__}: {exc}")
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        body.append("  调用栈：\n" + "\n".join("    " + line for line in detail.splitlines()))
    text = "\n".join(body) + "\n"
    print(text)
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
        print(f"错误详情已写入：{path}")
    except OSError:
        pass  # 日志都写不了就别再折腾了，至少 stdout 还有一份


def port_is_free(port):
    """按 Flask 开发服务器同样的方式试绑一次。

    必须真的 bind，而不是查进程列表：macOS 上 ControlCenter 监听的是 *:5000，
    我们绑 127.0.0.1:5000 其实能成功，光看 lsof 会误判成「端口被占」。
    Werkzeug 也设 SO_REUSEADDR，所以这里设同样的选项才有可比性。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def pick_port(start=PORT, tries=PORT_TRIES):
    """返回第一个可用端口；连续被占则抛 OSError（由调用方写日志）。"""
    for port in range(start, start + tries):
        if port_is_free(port):
            return port
    raise OSError(
        f"{start}~{start + tries - 1} 全部被占用，请关掉另一个 AI 业务台实例"
        f"（同一台机器同时只能开一个），或释放这些端口后重试。"
    )



def ensure_db():
    """首次运行时在可执行文件同级目录建库；已存在则原样保留数据。"""
    if os.path.exists(DB_PATH):
        return
    os.makedirs(DATA_DIR, exist_ok=True)
    # sqlite_init.py 源码运行时与本文件同目录；打包后在 PyInstaller 的解包目录里
    module_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)
    import sqlite_init

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        sqlite_init.create_schema(conn)
        sqlite_init.seed_dicts(conn)
        sqlite_init.seed_entities(conn)
        conn.commit()
    finally:
        conn.close()
    print(f"[初始化] 已创建数据库 {DB_PATH}")

# 之前造的假数据：外键链上的两个实体（见 db_test.py）
TEST_SOURCE = "TST-ENT-SRC"
TEST_TARGET = "TST-ENT-TGT"

# ② ECN 自动拦截生成的异常记录内容（取值已核对字典表，满足外键约束）
FRAG_SANDBOX = "BIZ_OP"
FRAG_TYPE = "FRAG-04"
FRAG_STATUS = "PENDING"
FRAG_SUMMARY = "系统自动拦截：该 ECN 变更导致成本增加 5 元。"

# ③ 自动裁决工单内容（CLOSED 在 T1_DICT_TICKET_STATUS 内，DISPOSAL_RESULT 列宽 16 字符够用）
TICKET_CONFLICT_DESC = "系统拦截：差额5元，等待财务审批。"
TICKET_ARBITRATOR = "财务总监-李四"
TICKET_DISPOSAL = "自动放行（合规额度内）"
TICKET_STATUS = "CLOSED"

# 三阶段在时间线视图里的同秒排序权重
STAGE_EVENT, STAGE_FRAG, STAGE_TICKET = 1, 2, 3


def get_conn():
    # 必须先建库再 connect：sqlite3.connect 会自动创建一个空的 .db 文件，
    # 那样 ensure_db() 会误判「库已存在」，后续查询全报 no such table。
    ensure_db()
    conn = sqlite3.connect(DB_PATH)
    # 外键强制必须每个连接单独开，而且要是第一条语句：
    # SQLite 默认不校验外键，且此时若已有事务在跑，设置会被静默忽略。
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _as_datetime(value):
    """把 SQLite 读回的时间文本还原成 datetime，让下面的 fmt_ts / fmt_hms 无需改动。"""
    if isinstance(value, str) and len(value) == 19 and value[4] == "-" and value[13] == ":":
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value
    return value


def query_all(sql, params=()):
    conn = get_conn()
    try:
        # 注意：sqlite3.Cursor 不支持 with（pymysql 的 Cursor 支持），
        # 这里直接取游标，close() 由外层 conn.close() 负责。
        cur = conn.cursor()
        cur.execute(sql, params)
        return [tuple(_as_datetime(v) for v in row) for row in cur.fetchall()]
    finally:
        conn.close()


def make_id(prefix):
    """时间前缀 + 随机后缀，肉眼可辨顺序，也避免撞主键。"""
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8].upper()}"


def run_ecn_chain():
    """一个事务跑通「ECN 事件 → 自动拦截异常 → 自动裁决工单」全链路。

    顺序：T4_E1_EVENT → T5_B1_FRAG（ASSET_CODE 挂事件目标实体）
          → T5_B0_TICKET（FRAG_ID 外键指向刚插的异常）。
    任一步失败整体回滚，不会留下半条链路；后两步引用的是本事务内刚插入的
    主键值，SQLite 同样能看见自己未提交的写入，外键校验不会失败（已实测）。
    占位符用 ?（MySQL 的 %s 在 SQLite 里会抛 OperationalError）。
    返回 (event_id, frag_id, ticket_id)。
    """
    event_id = make_id("EVT")
    frag_id = make_id("FRG")
    ticket_id = make_id("TKT")
    conn = get_conn()
    try:
        cur = conn.cursor()  # 不用 with：sqlite3.Cursor 不支持上下文管理器协议
        # ① 事件
        cur.execute(
            "INSERT INTO T4_E1_EVENT "
            "(EVENT_ID, EVENT_TYPE, EVENT_SOURCE, EVENT_TARGET) "
            "VALUES (?, ?, ?, ?)",
            (event_id, "BIZ", TEST_SOURCE, TEST_TARGET),
        )
        # ② 异常
        cur.execute(
            "INSERT INTO T5_B1_FRAG "
            "(FRAG_ID, SANDBOX_CODE, FRAG_TYPE, ASSET_CODE, CONTENT_SUMMARY, "
            " PROCESS_STATUS, REMARK) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                frag_id,
                FRAG_SANDBOX,
                FRAG_TYPE,
                TEST_TARGET,
                FRAG_SUMMARY,
                FRAG_STATUS,
                f"系统自动生成 · 关联事件 {event_id}",
            ),
        )
        # ③ 裁决工单：MySQL 的 NOW() 换成 datetime('now','localtime')，保持本地时间
        cur.execute(
            "INSERT INTO T5_B0_TICKET "
            "(TICKET_ID, FRAG_ID, ASSET_CODE, CONFLICT_DESC, ARBITRATOR, "
            " ARBITRATE_TIME, DISPOSAL_RESULT, RESP_SANDBOX, CLOSE_TIME, "
            " TICKET_STATUS, REMARK) "
            f"VALUES (?, ?, ?, ?, ?, {TS_NOW}, ?, ?, {TS_NOW}, ?, ?)",
            (
                ticket_id,
                frag_id,
                TEST_TARGET,
                TICKET_CONFLICT_DESC,
                TICKET_ARBITRATOR,
                TICKET_DISPOSAL,
                FRAG_SANDBOX,
                TICKET_STATUS,
                f"系统自动裁决 · 关联异常 {frag_id}",
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return event_id, frag_id, ticket_id


def fmt_ts(value):
    """CREATE_TIME 是 datetime 对象；NULL 或异常类型时返回空串，不渲染时间。"""
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value) if value else ""


def fmt_hms(value):
    """只取时分秒，用于卡片内展示裁决/关闭时刻。"""
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M:%S")
    return str(value) if value else ""


def _nid(value):
    """把 EVT-/FRG-/TKT- 这类 ID 包成不可断行的 span。

    ID 里有连字符，而 CSS 默认允许在连字符后断行（word-break: normal 也挡不住），
    所以横幅里 FRG-261019162450-C25E151C 会被劈成两截。光改 overflow-wrap 没用，
    必须用 white-space: nowrap 让整段 ID 作为一个整体换到下一行。
    """
    return f'<span class="id">{escape(str(value))}</span>'


def timeline_item(color, tag, title, subs, ts=""):
    """渲染时间轴上的一个节点。color: blue / red / green；ts 显示在卡片右上角。

    subs 里值为 NULL / 空串的行直接跳过（库里旧记录字段可能为 NULL，escape(None) 会抛异常）。
    """
    sub_html = "".join(
        f'<div class="sub"><span class="k">{escape(str(k))}</span>{escape(str(v))}</div>'
        for k, v in subs
        if k and v is not None and v != ""
    )
    ts_html = f'<span class="ts">{escape(ts)}</span>' if ts else ""
    return (
        f'<div class="item {color}">'
        f'<span class="dot"></span>'
        f'<div class="card">'
        f'{ts_html}'
        f'<div class="tag">{escape(tag)}</div>'
        f'<div class="main">{escape(title)}</div>'
        f'{sub_html}'
        f'</div></div>'
    )


def build_cards(events, frags, tickets, marks):
    """把三张表的查询结果转成 (create_time, stage_rank, html) 列表，供两种视图使用。"""
    cards = []

    for ev in events:
        tag = "① 事件 · T4_E1_EVENT" + ("（新增）" if ev[0] == marks["event"] else "")
        html = timeline_item(
            "blue", tag, ev[0],
            [("类型", ev[1]), ("来源", ev[2]), ("目标", ev[3])],
            fmt_ts(ev[4]),
        )
        cards.append((ev[4], STAGE_EVENT, ev[0], html))

    for fg in frags:
        tag = "② 异常 · T5_B1_FRAG" + ("（自动拦截 · 新增）" if fg[0] == marks["frag"] else "")
        html = timeline_item(
            "red", tag, fg[0],
            [
                ("内容摘要", fg[1]),
                ("状态", fg[2]),
                ("关联实体", fg[3]),
                ("来源", fg[4]),
            ],
            fmt_ts(fg[5]),
        )
        cards.append((fg[5], STAGE_FRAG, fg[0], html))

    for tk in tickets:
        tag = "③ 裁决 · T5_B0_TICKET" + ("（自动裁决 · 新增）" if tk[0] == marks["ticket"] else "")
        html = timeline_item(
            "green", tag, tk[0],
            [
                ("关联异常", tk[1]),
                ("冲突描述", tk[2]),
                ("仲裁人", tk[3]),
                ("处理结果", tk[4]),
                ("状态", tk[5]),
                ("裁决时刻", fmt_hms(tk[6])),
                ("关闭时刻", fmt_hms(tk[7])),
            ],
            fmt_ts(tk[8]),
        )
        cards.append((tk[8], STAGE_TICKET, tk[0], html))

    return cards


@app.route("/trigger", methods=["POST"])
def trigger():
    """模拟一次 ECN 变更：事件 + 自动拦截异常 + 自动裁决工单，然后回首页。"""
    try:
        event_id, frag_id, ticket_id = run_ecn_chain()
    except sqlite3.Error as exc:
        # 常见原因：库文件不存在或没跑过 sqlite_init.py、外键父行缺失、ID 重复
        return redirect(url_for("index", err=str(exc)[:200], view=request.form.get("view", "")))
    return redirect(url_for(
        "index", new=event_id, frag=frag_id, ticket=ticket_id,
        view=request.form.get("view", ""),
    ))


@app.route("/")
def index():
    # 三张表都有物理时间戳，按 CREATE_TIME 排序即真实插入顺序；
    # 同一秒内的记录用主键兜底，保证刷新后顺序稳定。
    events = query_all(
        "SELECT EVENT_ID, EVENT_TYPE, EVENT_SOURCE, EVENT_TARGET, CREATE_TIME "
        "FROM T4_E1_EVENT ORDER BY CREATE_TIME, EVENT_ID"
    )
    frags = query_all(
        "SELECT FRAG_ID, CONTENT_SUMMARY, PROCESS_STATUS, ASSET_CODE, REMARK, CREATE_TIME "
        "FROM T5_B1_FRAG ORDER BY CREATE_TIME, FRAG_ID"
    )
    tickets = query_all(
        "SELECT TICKET_ID, FRAG_ID, CONFLICT_DESC, ARBITRATOR, DISPOSAL_RESULT, "
        " TICKET_STATUS, ARBITRATE_TIME, CLOSE_TIME, CREATE_TIME "
        "FROM T5_B0_TICKET ORDER BY CREATE_TIME, TICKET_ID"
    )

    marks = {
        "event": request.args.get("new"),
        "frag": request.args.get("frag"),
        "ticket": request.args.get("ticket"),
    }
    cards = build_cards(events, frags, tickets, marks)

    view = request.args.get("view", "stage")
    if view == "time":
        # 时间线视图：三张表按发生时刻交错，蓝→红→绿天然连贯
        ordered = sorted(cards, key=lambda c: (c[0] or datetime.min, c[1], c[2]))
        items = "".join(c[3] for c in ordered)
    else:
        # 分组视图：按阶段分组（①全部事件 ②全部异常 ③全部裁决）
        items = "".join(c[3] for c in sorted(cards, key=lambda c: (c[1], c[0] or datetime.min, c[2])))

    if not items:
        items = timeline_item("blue", "时间轴", "暂无数据", [])

    other_view = "stage" if view == "time" else "time"
    other_label = "按阶段分组" if view == "time" else "按时间交错"

    err = request.args.get("err")
    banner = ""
    if marks["event"] and marks["frag"] and marks["ticket"]:
        banner = (
            f'<div class="banner ok">ECN 链路已闭环：<br>'
            f'事件 {_nid(marks["event"])} → 拦截异常 {_nid(marks["frag"])} '
            f'→ 裁决工单 {_nid(marks["ticket"])}</div>'
        )
    elif marks["event"]:
        banner = f'<div class="banner ok">已插入新事件 {_nid(marks["event"])}</div>'
    elif err:
        banner = f'<div class="banner bad">生成失败（已整体回滚）：{escape(err)}</div>'

    html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI MVP 数据链路 Timeline</title>
<style>
  body {{
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #f5f6f8; margin: 0; padding: 40px 20px; color: #333;
  }}
  .wrap {{ max-width: 620px; margin: 0 auto; }}
  h1 {{ font-size: 20px; text-align: center; margin-bottom: 8px; font-weight: 600; }}
  .hint {{ text-align: center; font-size: 13px; color: #888; margin-bottom: 12px; }}
  .hint a {{ color: #1e88e5; text-decoration: none; }}
  .hint a:hover {{ text-decoration: underline; }}
  .trigger {{ text-align: center; margin-bottom: 24px; }}
  .trigger button {{
    font: inherit; font-size: 14px; font-weight: 600;
    padding: 10px 22px; border: 0; border-radius: 6px;
    background: #1e88e5; color: #fff; cursor: pointer;
    box-shadow: 0 1px 3px rgba(30,136,229,.4);
  }}
  .trigger button:hover {{ background: #1976d2; }}
  .trigger button:active {{ transform: translateY(1px); }}
  .banner {{
    font-size: 13px; text-align: center; border-radius: 6px;
    padding: 9px 12px; margin-bottom: 20px; line-height: 1.7;
    /* break-all 会把 ID 从任意字符处劈开；这里只允许在空格处换行，
       并且 ID 由 .id 强制整体不折行（连字符在 CSS 里是合法断行点，
       单靠 word-break 挡不住，见 _nid）。 */
    word-break: normal; overflow-wrap: break-word;
  }}
  .banner .id {{ white-space: nowrap; }}
  .banner.ok {{ background: #e6f4ea; color: #1e7e34; }}
  .banner.bad {{ background: #fdeaea; color: #c62828; }}
  .timeline {{ position: relative; padding-left: 34px; }}
  .timeline::before {{
    content: ""; position: absolute; left: 10px; top: 8px; bottom: 8px;
    width: 2px; background: #d8dbe0;
  }}
  .item {{ position: relative; margin-bottom: 22px; }}
  .dot {{
    position: absolute; left: -31px; top: 7px; width: 12px; height: 12px;
    border-radius: 50%; border: 3px solid #fff;
    box-shadow: 0 0 0 2px currentColor; background: currentColor;
  }}
  .card {{
    position: relative;
    border-left: 4px solid; border-radius: 8px; padding: 14px 16px;
    background: #fff; box-shadow: 0 1px 3px rgba(0,0,0,.08);
  }}
  .ts {{
    position: absolute; top: 13px; right: 14px;
    font-size: 11px; color: #9a9a9a; white-space: nowrap;
  }}
  .tag {{ font-size: 12px; font-weight: 600; letter-spacing: 1px; padding-right: 118px; }}
  .main {{
    font-size: 16px; font-weight: 600; margin: 6px 0 4px;
    overflow-wrap: anywhere;
  }}
  .sub {{
    font-size: 13px; color: #555; line-height: 1.7;
    overflow-wrap: anywhere;
  }}
  .k {{
    color: #999; margin-right: 6px;
    display: inline-block; min-width: 4.5em;
  }}
  .blue {{ color: #1e88e5; }}
  .blue .card {{ border-color: #1e88e5; background: #eef5fe; }}
  .red  {{ color: #e53935; }}
  .red .card  {{ border-color: #e53935; background: #fdeeee; }}
  .green{{ color: #43a047; }}
  .green .card{{ border-color: #43a047; background: #edf7ee; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>AI MVP 数据链路</h1>
  <div class="hint">
    共 {len(events)} 条事件 · {len(frags)} 条异常 · {len(tickets)} 条裁决 ·
    <a href="{url_for('index', view=other_view)}">{other_label}</a>
  </div>
  <form class="trigger" method="post" action="/trigger">
    <input type="hidden" name="view" value="{escape(view)}">
    <button type="submit">模拟 ECN 变更发生</button>
  </form>
  {banner}
  <div class="timeline">
    {items}
  </div>
</div>
</body>
</html>"""
    return html


def _open_browser(port):
    webbrowser.open(f"http://{HOST}:{port}")


def main():
    """启动流程：建库 → 选端口 → 开服务，任何一步失败都落一份可读日志。"""
    try:
        ensure_db()
    except Exception as exc:
        write_error_log(
            "数据库初始化失败：常见原因是程序所在目录只读（从 DMG / 光盘 / "
            "NTFS U 盘里直接双击），或同级 data 文件夹没有写权限。",
            exc,
        )
        return 1
    try:
        port = pick_port()
    except Exception as exc:
        write_error_log("端口选择失败。", exc)
        return 1
    print(f"AI 业务台已启动：http://{HOST}:{port}\n数据库位置：{DB_PATH}")
    # debug=False 没有 reloader，进程只跑一次，定时器不会重复开标签页。
    # 若将来把 debug 改回 True，需要加 `os.environ.get("WERKZEUG_RUN_MAIN")` 判断，
    # 否则父子进程会各自打开一次浏览器。
    threading.Timer(1.0, _open_browser, args=(port,)).start()
    try:
        app.run(host=HOST, port=port, debug=False, threaded=True)
    except Exception as exc:
        # 探测和真正启动之间有极小的竞态（别人刚好抢了这个端口），兜底记一条。
        write_error_log(f"服务启动失败（端口 {port}）。", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
