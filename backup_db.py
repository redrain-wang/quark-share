"""
本地数据库每日备份
导出任务队列相关表到 backups/，保留最近 14 天

用法：python backup_db.py
运行器每天自动执行一次
"""
import gzip
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
BACKUP_DIR = BASE_DIR / "backups"
CONTAINER = "mysql-easysvip"
KEEP_DAYS = 14
TABLES = ["quark_transfer", "quark_link_vod", "cloud_accounts", "mac_vod_netdisk"]


def main():
    BACKUP_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out_file = BACKUP_DIR / f"easysvip_tasks_{stamp}.sql.gz"

    cmd = [
        "docker", "exec", CONTAINER,
        "mysqldump", "-uroot", "-peasysvip123",
        "--single-transaction", "--skip-lock-tables",
        "easysvip", *TABLES,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=300)
        if r.returncode != 0:
            print(f"❌ 备份失败: {r.stderr.decode()[:200]}")
            return False
        with gzip.open(out_file, "wb") as f:
            f.write(r.stdout)
        size_mb = out_file.stat().st_size / 1024 / 1024
        print(f"✅ 备份完成: {out_file.name} ({size_mb:.1f}MB)")
    except Exception as e:
        print(f"❌ 备份异常: {e}")
        return False

    # 清理过期备份
    cutoff = time.time() - KEEP_DAYS * 86400
    removed = 0
    for f in BACKUP_DIR.glob("easysvip_tasks_*.sql.gz"):
        if f.stat().st_mtime < cutoff:
            f.unlink()
            removed += 1
    if removed:
        print(f"  已清理 {removed} 个过期备份（保留 {KEEP_DAYS} 天）")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
