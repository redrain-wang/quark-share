"""
数据库初始化脚本（v2 - 按电影名转存方案）
运行此脚本创建/重建所有需要的表

使用方法：python init_db.py --rebuild  (删除旧表重建)
          python init_db.py           (只创建不存在的表)
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import aiomysql
from config import DB_CONFIG


# ============================================================
#  建表 SQL（v2 方案）
# ============================================================

TABLES = [
    # 1. 转存任务：每部电影（按名称去重）一条记录
    """
    CREATE TABLE IF NOT EXISTS quark_transfer (
        id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        vod_name      VARCHAR(255) NOT NULL COMMENT '影片名称（去重后唯一）',
        vod_year      VARCHAR(10)  NOT NULL DEFAULT '' COMMENT '年份',
        status        TINYINT      DEFAULT 0 COMMENT '0待处理 1已找到资源 2已转存 3已分享 4无资源 5转存失败',
        try_count     INT          DEFAULT 0 COMMENT '尝试次数',
        original_url  VARCHAR(500) DEFAULT '' COMMENT '转存来源的分享链接',
        original_pwd  VARCHAR(20)  DEFAULT '' COMMENT '来源提取码',
        quark_fid     VARCHAR(100) DEFAULT '' COMMENT '转存后的文件/文件夹 fid',
        my_url        VARCHAR(500) DEFAULT '' COMMENT '我生成的分享链接',
        my_pwd        VARCHAR(20)  DEFAULT '' COMMENT '我的提取码',
        error_msg     VARCHAR(500) DEFAULT '' COMMENT '失败原因',
        created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
        updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uk_name (vod_name),
        INDEX idx_status (status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='夸克转存任务（按影片名）'
    """,

    # 2. 分享链接与视频ID映射（一对多：一个链接对应多个 vod_id）
    """
    CREATE TABLE IF NOT EXISTS quark_link_vod (
        id          INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        transfer_id INT UNSIGNED NOT NULL COMMENT 'quark_transfer.id',
        vod_id      INT UNSIGNED NOT NULL COMMENT 'mac_vod.vod_id',
        created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
        UNIQUE KEY uk_transfer_vod (transfer_id, vod_id),
        INDEX idx_vod (vod_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='分享链接与视频ID映射'
    """,

    # 3. 网盘账号池（保留）
    """
    CREATE TABLE IF NOT EXISTS cloud_accounts (
        id         INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
        disk_type  VARCHAR(20)  NOT NULL COMMENT '网盘类型: quark/baidu',
        phone      VARCHAR(20)  NOT NULL COMMENT '登录手机号',
        password   VARCHAR(200) NOT NULL COMMENT '登录密码',
        cookie     TEXT         NULL COMMENT 'Cookie JSON',
        status     TINYINT      DEFAULT 1 COMMENT '1正常 0失效',
        last_used  DATETIME     NULL,
        created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_disk_type (disk_type, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='网盘账号池'
    """,
]

# 旧方案遗留的表（可安全删除，注意外键顺序：saved_links 依赖 pending_links）
OLD_TABLES = ["saved_links", "pending_links", "keyword_logs"]


async def init_database(rebuild: bool = False):
    """连接数据库并执行建表"""
    print(f"连接数据库: {DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}")

    conn = await aiomysql.connect(
        host=DB_CONFIG["host"],
        port=DB_CONFIG["port"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        db=DB_CONFIG["database"],
        charset="utf8mb4",
        autocommit=True,
    )

    try:
        async with conn.cursor() as cur:
            if rebuild:
                # 删除旧表
                for t in OLD_TABLES + ["quark_transfer", "quark_link_vod"]:
                    await cur.execute(f"DROP TABLE IF EXISTS {t}")
                print("旧表已删除")

            for i, sql in enumerate(TABLES, 1):
                await cur.execute(sql)
                print(f"  表 {i}/{len(TABLES)} 创建/验证成功")

        print("\n数据库初始化完成!")
    except Exception as e:
        print(f"\n错误: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    rebuild = "--rebuild" in sys.argv
    asyncio.run(init_database(rebuild=rebuild))
