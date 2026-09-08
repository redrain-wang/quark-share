"""
清理 easysvip.com 文件夹中的重复转存文件
规则：同名文件/文件夹的 (1) (2) 等后缀副本，保留最早的一个，删除其余

使用方法：
  python cleanup_dups.py          # 预览要删除的重复项
  python cleanup_dups.py --apply  # 实际执行删除
"""
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from quark_api import QuarkAPI, load_cookie_header, QuarkAPIError

FOLDER_FID = "1cba61a854d847ddaffe74db44a9fd24"  # easysvip.com


def normalize(s: str) -> str:
    s = re.sub(r"\(\d+\)", "", s)
    s = re.sub(r"[\s（）()·：:【】\[\]]", "", s)
    return s.lower()


async def main(apply: bool):
    cookie = load_cookie_header("browser_data/quark_1_cookies.json")
    api = QuarkAPI(cookie)
    try:
        files = await api.list_folder_files(FOLDER_FID)
        print(f"easysvip.com 文件夹内共 {len(files)} 项\n")

        # 按归一化名称分组
        groups: dict[str, list[dict]] = {}
        for f in files:
            key = normalize(f["file_name"])
            groups.setdefault(key, []).append(f)

        to_delete = []
        for key, items in groups.items():
            if len(items) < 2:
                continue
            # 保留 fid 最小的（最早转存的），删除其余
            items_sorted = sorted(items, key=lambda x: x["fid"])
            keep = items_sorted[0]
            dups = items_sorted[1:]
            print(f"重复组 [{keep['file_name'][:40]}]")
            print(f"  保留: {keep['fid'][:12]}...")
            for d in dups:
                print(f"  删除: {d['file_name'][:40]} ({d['fid'][:12]}...)")
                to_delete.append(d["fid"])

        if not to_delete:
            print("\n没有重复项")
            return

        print(f"\n共 {len(to_delete)} 个重复项待删除")
        if not apply:
            print("预览模式，加 --apply 执行删除")
            return

        # 分批删除（每批最多 20 个）
        for i in range(0, len(to_delete), 20):
            batch = to_delete[i:i + 20]
            try:
                await api.delete_files(batch)
                print(f"已删除 {len(batch)} 个")
            except QuarkAPIError as e:
                print(f"删除失败: {e}")
            await asyncio.sleep(2)

        print("\n清理完成!")
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main(apply="--apply" in sys.argv))
