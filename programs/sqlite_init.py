"""创建 SQLite 版数据库 data/mvp.db，等价复刻 ai_mvp_v3 中本项目用到的表。

包含三张业务表 T4_E1_EVENT / T5_B1_FRAG / T5_B0_TICKET，以及它们外键链上的父表
（字典表 + T1_A1_ROOT → T3_A2_MID → T3_A3_ENTITY）。父表必须一起建，否则
PRAGMA foreign_keys=ON 之下插入事件/异常/工单会直接被外键拦下。

用法：
    python3 programs/sqlite_init.py              # 建库 + 种父表数据 + 自检
    python3 programs/sqlite_init.py --fresh      # 先删掉已有 mvp.db 再重建
    python3 programs/sqlite_init.py --schema-only  # 只建表，不种 root/mid/entity 链
    python3 programs/sqlite_init.py --from-mysql   # 额外把 MySQL 现有业务数据导过来
                                                   # （需要 pymysql 能连上 MySQL）

MySQL → SQLite 的语法差异（均在本机 Python 3.12.3 / SQLite 3.45.1 实测确认）：
  1. 占位符 %s → ?（用 %s 会抛 sqlite3.OperationalError）。
  2. NOW() → datetime('now','localtime')；CURRENT_TIMESTAMP 在 SQLite 里是 UTC，
     本机实测 14:44 时它返回 06:44，直接用会让页面时间比实际早 8 小时。
  3. 表达式默认值必须加括号：DEFAULT (datetime('now','localtime'))。
  4. 外键约束默认不启用，且每个连接都要单独开；事务已存在时设置会被静默忽略。
     所以本文件和 web_app.py 都在连接建立后的第一条语句就执行 PRAGMA foreign_keys=ON。
  5. 列宽不生效：varchar(32) 能塞进任意长度的值，MySQL 那边会报错或截断。
     NOT NULL / 主键 / 外键则照常强制。
  6. DATETIME 列读回是 str 而不是 datetime 对象，web_app.py 的转换层已处理。
"""

import os
import sqlite3
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.path.dirname(HERE), "data", "mvp.db")

MYSQL_CONFIG = dict(
    host=os.environ.get("MYSQL_HOST", "localhost"),
    port=3306,
    user=os.environ.get("MYSQL_USER", "root"),
    # 密码不写进源码：这个文件会被 PyInstaller 打进要分发的包里，仓库也可能被推到
    # 第三方托管。需要迁移时用环境变量传：
    #   MYSQL_PWD=你的密码 python3 programs/sqlite_init.py --from-mysql
    password=os.environ.get("MYSQL_PWD", ""),
    database=os.environ.get("MYSQL_DB", "ai_mvp_v3"),
    charset="utf8mb4",
)

TS_DEFAULT = "DATETIME NOT NULL DEFAULT (datetime('now','localtime'))"

# 建表顺序即外键依赖顺序
DDL = [
    # ---- 字典表：本项目只用到主键列，故只建主键（列名取自 MySQL 的外键定义）----
    ("T1_DICT_SANDBOX",
     "CREATE TABLE IF NOT EXISTS T1_DICT_SANDBOX ("
     "SANDBOX_CODE VARCHAR(32) PRIMARY KEY)"),
    ("T1_DICT_BIZ_TYPE",
     "CREATE TABLE IF NOT EXISTS T1_DICT_BIZ_TYPE ("
     "BIZ_TYPE_CODE VARCHAR(16) PRIMARY KEY)"),
    ("T1_DICT_SMO_LEVEL",
     "CREATE TABLE IF NOT EXISTS T1_DICT_SMO_LEVEL ("
     "SMO_LEVEL_CODE VARCHAR(16) PRIMARY KEY)"),
    ("T1_DICT_FRAG_TYPE",
     "CREATE TABLE IF NOT EXISTS T1_DICT_FRAG_TYPE ("
     "FRAG_TYPE_CODE VARCHAR(16) PRIMARY KEY)"),
    ("T1_DICT_FRAG_MATURITY",
     "CREATE TABLE IF NOT EXISTS T1_DICT_FRAG_MATURITY ("
     "MATURITY_CODE VARCHAR(16) PRIMARY KEY)"),
    ("T1_DICT_TICKET_STATUS",
     "CREATE TABLE IF NOT EXISTS T1_DICT_TICKET_STATUS ("
     "TICKET_STATUS_CODE VARCHAR(16) PRIMARY KEY)"),

    # ---- 实体链：T1_A1_ROOT → T3_A2_MID → T3_A3_ENTITY ----
    ("T1_A1_ROOT",
     "CREATE TABLE IF NOT EXISTS T1_A1_ROOT ("
     "ROOT_ID VARCHAR(32) PRIMARY KEY, ROOT_NAME VARCHAR(64), "
     "SANDBOX_CODE VARCHAR(32) NOT NULL, SYNC_SCOPE VARCHAR(32), "
     "CORE_CONSTRAINT VARCHAR(64), "
     "FOREIGN KEY (SANDBOX_CODE) REFERENCES T1_DICT_SANDBOX(SANDBOX_CODE))"),
    ("T3_A2_MID",
     "CREATE TABLE IF NOT EXISTS T3_A2_MID ("
     "MID_ID VARCHAR(32) PRIMARY KEY, MID_NAME VARCHAR(64), "
     "MID_CATEGORY VARCHAR(32), ROOT_ID VARCHAR(32) NOT NULL, "
     "SANDBOX_CODE VARCHAR(32) NOT NULL, "
     "FOREIGN KEY (ROOT_ID) REFERENCES T1_A1_ROOT(ROOT_ID), "
     "FOREIGN KEY (SANDBOX_CODE) REFERENCES T1_DICT_SANDBOX(SANDBOX_CODE))"),
    ("T3_A3_ENTITY",
     "CREATE TABLE IF NOT EXISTS T3_A3_ENTITY ("
     "ENTITY_ID VARCHAR(32) PRIMARY KEY, ENTITY_NAME VARCHAR(64), "
     "SANDBOX_CODE VARCHAR(32) NOT NULL, BIZ_TYPE_CODE VARCHAR(16) NOT NULL, "
     "SMO_LEVEL_CODE VARCHAR(16) NOT NULL, MID_ID VARCHAR(32) NOT NULL, "
     "FOREIGN KEY (SANDBOX_CODE) REFERENCES T1_DICT_SANDBOX(SANDBOX_CODE), "
     "FOREIGN KEY (BIZ_TYPE_CODE) REFERENCES T1_DICT_BIZ_TYPE(BIZ_TYPE_CODE), "
     "FOREIGN KEY (SMO_LEVEL_CODE) REFERENCES T1_DICT_SMO_LEVEL(SMO_LEVEL_CODE), "
     "FOREIGN KEY (MID_ID) REFERENCES T3_A2_MID(MID_ID))"),

    # ---- ① 事件 ----
    ("T4_E1_EVENT",
     "CREATE TABLE IF NOT EXISTS T4_E1_EVENT ("
     "EVENT_ID VARCHAR(32) PRIMARY KEY, EVENT_TYPE VARCHAR(16) NOT NULL, "
     "EVENT_SOURCE VARCHAR(32) NOT NULL, EVENT_TARGET VARCHAR(32) NOT NULL, "
     f"CREATE_TIME {TS_DEFAULT}, "
     "FOREIGN KEY (EVENT_SOURCE) REFERENCES T3_A3_ENTITY(ENTITY_ID), "
     "FOREIGN KEY (EVENT_TARGET) REFERENCES T3_A3_ENTITY(ENTITY_ID))"),

    # ---- ② 异常（列清单与 MySQL 的 16 列一致，外键 6 条）----
    ("T5_B1_FRAG",
     "CREATE TABLE IF NOT EXISTS T5_B1_FRAG ("
     "FRAG_ID VARCHAR(32) PRIMARY KEY, SANDBOX_CODE VARCHAR(32) NOT NULL, "
     "FRAG_TYPE VARCHAR(16), ASSET_CODE VARCHAR(48), CONTENT_SUMMARY TEXT, "
     "FRAG_MATURITY VARCHAR(16), PROCESS_STATUS VARCHAR(16), "
     "SUBMITTER VARCHAR(32), SUBMIT_TIME DATETIME, HANDLER VARCHAR(32), "
     "HANDLE_TIME DATETIME, TICKET_STATUS VARCHAR(16), "
     "DIVERSITY_WEIGHT DECIMAL(3,2), ALT_SCHEME_LIST TEXT, REMARK TEXT, "
     f"CREATE_TIME {TS_DEFAULT}, "
     "FOREIGN KEY (SANDBOX_CODE) REFERENCES T1_DICT_SANDBOX(SANDBOX_CODE), "
     "FOREIGN KEY (FRAG_TYPE) REFERENCES T1_DICT_FRAG_TYPE(FRAG_TYPE_CODE), "
     "FOREIGN KEY (ASSET_CODE) REFERENCES T3_A3_ENTITY(ENTITY_ID), "
     "FOREIGN KEY (FRAG_MATURITY) REFERENCES T1_DICT_FRAG_MATURITY(MATURITY_CODE), "
     "FOREIGN KEY (PROCESS_STATUS) REFERENCES T1_DICT_TICKET_STATUS(TICKET_STATUS_CODE), "
     "FOREIGN KEY (TICKET_STATUS) REFERENCES T1_DICT_TICKET_STATUS(TICKET_STATUS_CODE))"),

    # ---- ③ 裁决工单（16 列，外键 4 条）----
    ("T5_B0_TICKET",
     "CREATE TABLE IF NOT EXISTS T5_B0_TICKET ("
     "TICKET_ID VARCHAR(32) PRIMARY KEY, FRAG_ID VARCHAR(32), "
     "CONFLICT_ID VARCHAR(32), ASSET_CODE VARCHAR(48), CONFLICT_DESC TEXT, "
     "ARBITRATOR VARCHAR(32), ARBITRATE_TIME DATETIME, ARBITRATE_BASIS TEXT, "
     "DISPOSAL_RESULT VARCHAR(16), RESP_SANDBOX VARCHAR(32), "
     "RECTIFY_DEADLINE VARCHAR(16), REVIEW_RESULT VARCHAR(16), "
     "CLOSE_TIME DATETIME, TICKET_STATUS VARCHAR(16), REMARK TEXT, "
     f"CREATE_TIME {TS_DEFAULT}, "
     "FOREIGN KEY (FRAG_ID) REFERENCES T5_B1_FRAG(FRAG_ID), "
     "FOREIGN KEY (ASSET_CODE) REFERENCES T3_A3_ENTITY(ENTITY_ID), "
     "FOREIGN KEY (RESP_SANDBOX) REFERENCES T1_DICT_SANDBOX(SANDBOX_CODE), "
     "FOREIGN KEY (TICKET_STATUS) REFERENCES T1_DICT_TICKET_STATUS(TICKET_STATUS_CODE))"),
]

# 种子数据：取值与 MySQL 侧一致（见 .workbuddy 日志与 db_test.py）
SEEDS = [
    ("T1_DICT_SANDBOX", "SANDBOX_CODE",
     ["BIZ_BIZ", "BIZ_OP", "ENG", "FDE_ARCH", "UPPER_ARB"]),
    ("T1_DICT_BIZ_TYPE", "BIZ_TYPE_CODE", ["ALL", "LS", "ZZ"]),
    ("T1_DICT_SMO_LEVEL", "SMO_LEVEL_CODE", ["M", "O", "S"]),
    ("T1_DICT_FRAG_TYPE", "FRAG_TYPE_CODE", [f"FRAG-{i:02d}" for i in range(1, 10)]),
    ("T1_DICT_TICKET_STATUS", "TICKET_STATUS_CODE",
     ["CLOSED", "PENDING", "PROCESSING", "REJECTED", "REVIEWING"]),
    # T1_DICT_FRAG_MATURITY 的取值本项目没用到（插异常时不填 FRAG_MATURITY，
    # NULL 不触发外键），留空表。要用的话得先把 MySQL 里的 MATURITY_CODE 补进来。
]

ENTITY_SEEDS = [
    ("INSERT OR IGNORE INTO T1_A1_ROOT "
     "(ROOT_ID, ROOT_NAME, SANDBOX_CODE, SYNC_SCOPE, CORE_CONSTRAINT) "
     "VALUES ('TST-ROOT', '测试根', 'BIZ_BIZ', 'ALL', 'TEST')"),
    ("INSERT OR IGNORE INTO T3_A2_MID "
     "(MID_ID, MID_NAME, MID_CATEGORY, ROOT_ID, SANDBOX_CODE) "
     "VALUES ('TST-MID', '测试中间件', 'TEST', 'TST-ROOT', 'BIZ_BIZ')"),
    ("INSERT OR IGNORE INTO T3_A3_ENTITY "
     "(ENTITY_ID, ENTITY_NAME, SANDBOX_CODE, BIZ_TYPE_CODE, SMO_LEVEL_CODE, MID_ID) "
     "VALUES ('TST-ENT-SRC', '测试来源实体', 'BIZ_BIZ', 'ALL', 'M', 'TST-MID')"),
    ("INSERT OR IGNORE INTO T3_A3_ENTITY "
     "(ENTITY_ID, ENTITY_NAME, SANDBOX_CODE, BIZ_TYPE_CODE, SMO_LEVEL_CODE, MID_ID) "
     "VALUES ('TST-ENT-TGT', '测试目标实体', 'BIZ_BIZ', 'ALL', 'M', 'TST-MID')"),
]

BUSINESS_TABLES = ["T4_E1_EVENT", "T5_B1_FRAG", "T5_B0_TICKET"]


def connect(path=DB_PATH):
    """新建连接；外键 pragma 必须是第一条语句，否则事务已开时会静默失效。"""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn):
    for name, sql in DDL:
        conn.execute(sql)
        print(f"  [建表] {name}")


def seed_dicts(conn):
    for table, pk, values in SEEDS:
        for v in values:
            conn.execute(f"INSERT OR IGNORE INTO {table}({pk}) VALUES(?)", (v,))
        print(f"  [种子] {table}: {len(values)} 条")


def seed_entities(conn):
    for sql in ENTITY_SEEDS:
        conn.execute(sql)
    print("  [种子] TST-ROOT / TST-MID / TST-ENT-SRC / TST-ENT-TGT")


def self_test(conn):
    """跑一遍 web_app 的三步链路，验证外键与默认值，最后回滚不留数据。"""
    stamp = datetime.now().strftime("%y%m%d%H%M%S")
    eid, fid, tid = (f"SELFTEST-EVT-{stamp}", f"SELFTEST-FRG-{stamp}", f"SELFTEST-TKT-{stamp}")
    conn.execute(
        "INSERT INTO T4_E1_EVENT (EVENT_ID, EVENT_TYPE, EVENT_SOURCE, EVENT_TARGET) "
        "VALUES (?, ?, ?, ?)", (eid, "BIZ", "TST-ENT-SRC", "TST-ENT-TGT"))
    conn.execute(
        "INSERT INTO T5_B1_FRAG (FRAG_ID, SANDBOX_CODE, FRAG_TYPE, ASSET_CODE, "
        "CONTENT_SUMMARY, PROCESS_STATUS, REMARK) VALUES (?,?,?,?,?,?,?)",
        (fid, "BIZ_OP", "FRAG-04", "TST-ENT-TGT",
         "系统自动拦截：该 ECN 变更导致成本增加 5 元。", "PENDING", f"关联事件 {eid}"))
    conn.execute(
        "INSERT INTO T5_B0_TICKET (TICKET_ID, FRAG_ID, ASSET_CODE, CONFLICT_DESC, "
        "ARBITRATOR, ARBITRATE_TIME, DISPOSAL_RESULT, RESP_SANDBOX, CLOSE_TIME, "
        "TICKET_STATUS, REMARK) VALUES (?,?,?,?,?,datetime('now','localtime'),"
        "?,?,datetime('now','localtime'),?,?)",
        (tid, fid, "TST-ENT-TGT", "系统拦截：差额5元，等待财务审批。", "财务总监-李四",
         "自动放行（合规额度内）", "BIZ_OP", "CLOSED", f"关联异常 {fid}"))
    row = conn.execute(
        "SELECT EVENT_ID, CREATE_TIME FROM T4_E1_EVENT WHERE EVENT_ID=?", (eid,)).fetchone()
    print(f"  [自检] 三步链路插入成功，CREATE_TIME 默认值 = {row[1]}（本地时间）")
    conn.rollback()
    left = conn.execute("SELECT count(*) FROM T4_E1_EVENT").fetchone()[0]
    print(f"  [自检] 已回滚，T4_E1_EVENT 残留 {left} 行")


def check_fk(conn):
    bad = conn.execute("PRAGMA foreign_key_check").fetchall()
    print(f"  [校验] foreign_key_check 违规 {len(bad)} 条")
    return not bad


def copy_from_mysql(conn):
    """把 MySQL 三张业务表的现有数据搬到 SQLite（只读 MySQL，写本地文件）。"""
    import pymysql
    my = pymysql.connect(**MYSQL_CONFIG)
    try:
        for table in BUSINESS_TABLES:
            with my.cursor() as cur:
                cur.execute(f"SELECT * FROM `{table}`")
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
            placeholders = ",".join(["?"] * len(cols))
            conn.execute(f"DELETE FROM {table}")  # 保证可重复执行
            for r in rows:
                # MySQL 的 datetime 对象转成与 DEFAULT 一致的文本格式
                conn.execute(
                    f"INSERT OR IGNORE INTO {table}({','.join(cols)}) "
                    f"VALUES ({placeholders})",
                    tuple(v.strftime("%Y-%m-%d %H:%M:%S") if isinstance(v, datetime) else v
                          for v in r))
            got = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"  [导入] {table}: MySQL {len(rows)} 行 → SQLite {got} 行")
    finally:
        my.close()


def main(argv):
    fresh = "--fresh" in argv
    schema_only = "--schema-only" in argv
    from_mysql = "--from-mysql" in argv

    if fresh and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[清理] 已删除旧库 {DB_PATH}")
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    print(f"[目标] {DB_PATH}")

    conn = connect()
    try:
        create_schema(conn)
        seed_dicts(conn)
        if not schema_only:
            seed_entities(conn)
        conn.commit()
        if from_mysql:
            copy_from_mysql(conn)
            conn.commit()
        ok = check_fk(conn)
        if not schema_only:
            self_test(conn)
        print("\n完成。启动页面：python3 programs/web_app.py  → http://127.0.0.1:5000")
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
