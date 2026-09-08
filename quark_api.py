"""
夸克网盘纯 API 客户端
不依赖浏览器，直接调用夸克 Web 端的 REST API

流程：
1. get_stoken:        获取分享链接的访问令牌
2. list_share_files:  列出分享中的文件
3. save_files:        转存文件到自己的网盘
4. wait_task:         等待转存任务完成
5. create_share:      为转存后的文件生成自己的分享链接
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path

import aiohttp
from yarl import URL

logger = logging.getLogger(__name__)

# API 基础地址
API_DRIVE = "https://drive-pc.quark.cn"
API_DRIVE_H = "https://drive-h.quark.cn"
SHARE_PAGE = "https://pan.quark.cn"

COMMON_PARAMS = "pr=ucpro&fr=pc"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


class QuarkAPIError(Exception):
    """夸克 API 异常"""
    def __init__(self, message: str, code: int = -1, after_transfer: bool = False,
                 saved_fids: list | None = None):
        super().__init__(message)
        self.code = code
        # True = 文件已转存成功，分享环节失败（重试时应走补分享，不再重复转存）
        self.after_transfer = after_transfer
        # 转存后的文件 fid（分享失败时用于对账/补分享）
        self.saved_fids = saved_fids or []


def load_cookies(cookie_file: str | Path) -> dict:
    """从 JSON 文件加载 Cookie（browser_cookie3 导出格式）"""
    raw = json.loads(Path(cookie_file).read_text())
    cookies = {}
    for c in raw:
        if "quark" in c.get("domain", ""):
            cookies[c["name"]] = c["value"]
    return cookies


def load_cookie_header(cookie_file: str | Path) -> str:
    """生成 Cookie 请求头字符串"""
    cookies = load_cookies(cookie_file)
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


class QuarkAPI:
    """夸克网盘 API 客户端"""

    def __init__(self, cookie_header: str):
        # 保序的 Cookie 字典（Chrome 原始顺序，WAF 对格式敏感）
        self._cookies: dict[str, str] = {}
        for pair in cookie_header.split("; "):
            if "=" in pair:
                k, v = pair.split("=", 1)
                self._cookies[k.strip()] = v.strip()
        self._session: aiohttp.ClientSession | None = None

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self._cookies.items())

    def _update_cookies_from_response(self, resp: aiohttp.ClientResponse):
        """
        解析响应的 Set-Cookie 并更新 Cookie 字典。
        夸克的 save 等接口会轮换 __puus，不更新会导致后续请求 401。
        """
        set_cookies = resp.headers.getall("Set-Cookie", [])
        if not set_cookies:
            return
        from http.cookies import SimpleCookie
        updated = False
        for sc in set_cookies:
            c = SimpleCookie()
            try:
                c.load(sc)
            except Exception:
                continue
            for key, morsel in c.items():
                if key in self._cookies and morsel.value and morsel.value != self._cookies[key]:
                    self._cookies[key] = morsel.value
                    updated = True
                    logger.debug(f"[QuarkAPI] Cookie 轮换: {key}")
        if updated:
            self._session.headers["Cookie"] = self._cookie_header()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # 注意：
            # 1. 必须用静态 Cookie 头（保持 Chrome 原始顺序/格式），
            #    aiohttp CookieJar 会重排/转义 Cookie 值，触发夸克 WAF 401
            # 2. Cookie 轮换通过 _update_cookies_from_response 手动处理
            # 3. 必须带完整浏览器指纹头（sec-fetch-*/Origin/Accept 等），
            #    缺少会被 WAF 识别为非浏览器并挑战敏感 POST（401）
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": UA,
                    "Cookie": self._cookie_header(),
                    "Referer": f"{SHARE_PAGE}/",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Origin": SHARE_PAGE,
                    "Sec-Fetch-Site": "same-site",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Dest": "empty",
                },
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def _request(self, method: str, url: str, **kwargs) -> dict:
        session = await self._get_session()
        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with session.request(method, url, **kwargs) as resp:
                    self._update_cookies_from_response(resp)
                    text = await resp.text()
                    if not text.strip():
                        last_err = QuarkAPIError(
                            f"响应为空 (HTTP {resp.status})"
                        )
                    else:
                        try:
                            return json.loads(text)
                        except json.JSONDecodeError as e:
                            last_err = QuarkAPIError(
                                f"响应解析失败 (HTTP {resp.status}, "
                                f"CT={resp.headers.get('Content-Type')}): "
                                f"{text[:150]!r}"
                            )
            except aiohttp.ClientError as e:
                last_err = QuarkAPIError(f"请求失败: {e}")
            if attempt < 2:
                await asyncio.sleep(1)
        raise last_err or QuarkAPIError("请求失败")

    # ================================================================
    #  1. 分享链接访问令牌
    # ================================================================

    async def get_stoken(self, pwd_id: str, passcode: str = "") -> str:
        """
        获取分享链接的 stoken。
        pwd_id 是分享链接 https://pan.quark.cn/s/{pwd_id} 中的 ID。
        提取码错误会抛出 QuarkAPIError。
        """
        url = f"{API_DRIVE_H}/1/clouddrive/share/sharepage/token?{COMMON_PARAMS}"
        payload = {
            "pwd_id": pwd_id,
            "passcode": passcode,
            "support_visit_limit_private_share": True,
        }
        data = await self._request("POST", url, json=payload)

        if data.get("code") != 0:
            msg = data.get("message", "")
            # 常见错误：提取码错误 / 链接失效
            if "提取码" in msg or "passcode" in msg.lower():
                raise QuarkAPIError(f"提取码错误: {msg}", code=data.get("code", -1))
            if "不存在" in msg or "失效" in msg or "取消" in msg:
                raise QuarkAPIError(f"链接无效: {msg}", code=data.get("code", -1))
            raise QuarkAPIError(f"获取stoken失败: {msg}", code=data.get("code", -1))

        stoken = data["data"]["stoken"]
        logger.debug(f"[QuarkAPI] 获取 stoken 成功: {pwd_id}")
        return stoken

    # ================================================================
    #  2. 分享文件列表
    # ================================================================

    async def list_share_files(
        self, pwd_id: str, stoken: str, pdir_fid: str = "0"
    ) -> list[dict]:
        """列出分享中的文件（含 fid 和 fid_token，转存时需要）"""
        url = f"{API_DRIVE}/1/clouddrive/share/sharepage/detail"
        params = {
            "pr": "ucpro",
            "fr": "pc",
            "pwd_id": pwd_id,
            "stoken": stoken,  # aiohttp 会自动 URL 编码
            "pdir_fid": pdir_fid,
            "_page": "1",
            "_size": "100",
            "_fetch_banner": "0",
            "_fetch_share": "0",
            "_fetch_total": "1",
            "_sort": "file_type:asc,file_name:asc",
        }
        data = await self._request("GET", url, params=params)

        if data.get("code") != 0:
            raise QuarkAPIError(
                f"获取文件列表失败: {data.get('message')}",
                code=data.get("code", -1),
            )

        files = data.get("data", {}).get("list", [])
        logger.debug(f"[QuarkAPI] 分享内文件数: {len(files)}")
        return files

    # ================================================================
    #  3. 转存文件
    # ================================================================

    async def save_files(
        self,
        pwd_id: str,
        stoken: str,
        fid_list: list[str],
        fid_token_list: list[str],
        to_pdir_fid: str,
    ) -> list[str]:
        """
        转存分享文件到自己的网盘。
        返回转存后的文件 fid 列表（用于后续分享）。
        """
        url = f"{API_DRIVE}/1/clouddrive/share/sharepage/save?{COMMON_PARAMS}"
        payload = {
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "to_pdir_fid": to_pdir_fid,
            "pwd_id": pwd_id,
            "stoken": stoken,
            "pdir_fid": "0",
            "scene": "link",
        }
        data = await self._request("POST", url, json=payload)

        if data.get("code") != 0:
            msg = data.get("message", "")
            if "空间" in msg:
                raise QuarkAPIError(f"网盘空间不足: {msg}", code=data.get("code", -1))
            raise QuarkAPIError(f"转存失败: {msg}", code=data.get("code", -1))

        task_id = data.get("data", {}).get("task_id")
        if not task_id:
            raise QuarkAPIError("转存未返回 task_id")

        # 等待任务完成
        saved_fids = await self.wait_task(task_id)
        return saved_fids

    async def wait_task(self, task_id: str, timeout: int = 60) -> list[str]:
        """轮询转存任务直到完成，返回保存后的文件 fid 列表"""
        url = f"{API_DRIVE}/1/clouddrive/task"
        start = time.time()
        retry_index = 0

        while time.time() - start < timeout:
            params = {
                "pr": "ucpro", "fr": "pc",
                "task_id": task_id, "retry_index": retry_index,
            }
            data = await self._request("GET", url, params=params)
            retry_index += 1

            task_data = data.get("data", {})
            status = task_data.get("status")

            if status == 2:  # 已完成
                save_as = task_data.get("save_as", {})
                fids = save_as.get("save_as_top_fids", [])
                logger.info(f"[QuarkAPI] 转存任务完成, 保存文件数: {len(fids)}")
                return fids

            # 其他状态（1=运行中 或未知）继续轮询，直到超时
            await asyncio.sleep(1)

        raise QuarkAPIError("转存任务超时")

    # ================================================================
    #  4. 文件夹操作
    # ================================================================

    async def get_or_create_folder(self, folder_name: str, parent_fid: str = "0") -> str:
        """
        获取指定名称文件夹的 fid，不存在则创建。
        返回文件夹 fid。
        """
        # 先查找
        url = (
            f"{API_DRIVE}/1/clouddrive/file/sort?{COMMON_PARAMS}"
            f"&pdir_fid={parent_fid}&_page=1&_size=100"
            f"&_fetch_total=0&_fetch_sub_dirs=0&_sort=file_type:asc,file_name:asc"
        )
        data = await self._request("GET", url)
        if data.get("code") == 0:
            for f in data.get("data", {}).get("list", []):
                if f.get("file_name") == folder_name and f.get("dir") is True:
                    logger.debug(f"[QuarkAPI] 找到文件夹: {folder_name} (fid={f['fid']})")
                    return f["fid"]

        # 不存在则创建
        url = f"{API_DRIVE}/1/clouddrive/file?{COMMON_PARAMS}"
        payload = {
            "pdir_fid": parent_fid,
            "file_name": folder_name,
            "dir_path": "",
            "dir_init_lock": False,
        }
        data = await self._request("POST", url, json=payload)
        if data.get("code") != 0:
            raise QuarkAPIError(f"创建文件夹失败: {data.get('message')}")

        fid = data["data"]["fid"]
        logger.info(f"[QuarkAPI] 已创建文件夹: {folder_name} (fid={fid})")
        return fid

    async def list_folder_files(self, folder_fid: str) -> list[dict]:
        """列出指定文件夹内的文件/子文件夹"""
        url = f"{API_DRIVE}/1/clouddrive/file/sort"
        params = {
            "pr": "ucpro", "fr": "pc",
            "pdir_fid": folder_fid,
            "_page": "1", "_size": "200",
            "_fetch_total": "0", "_fetch_sub_dirs": "0",
            "_sort": "file_type:asc,file_name:asc",
        }
        data = await self._request("GET", url, params=params)
        if data.get("code") != 0:
            raise QuarkAPIError(f"列目录失败: {data.get('message')}")
        return data.get("data", {}).get("list", [])

    async def find_existing_file(self, folder_fid: str, name: str) -> dict | None:
        """
        在目标文件夹中模糊查找同名文件/文件夹（防重复转存）。
        匹配规则（应对分享者的混淆命名，如 南z京z照z相z馆 / B-捕-FENG-追-YING）：
        1. 只提取中文字符后做包含匹配
        2. 原始归一化（去空格/标点/重复后缀）后包含匹配
        返回 {"fid":..., "file_name":...} 或 None。
        """
        import re

        def cjk_only(s: str) -> str:
            return "".join(re.findall(r"[\u4e00-\u9fff]", s or ""))

        def normalize(s: str) -> str:
            s = re.sub(r"\(\d+\)", "", s or "")
            s = re.sub(r"[\s（）()·：:【】\[\]]", "", s)
            return s.lower()

        target_cjk = cjk_only(name)
        target_norm = normalize(name)
        try:
            files = await self.list_folder_files(folder_fid)
        except QuarkAPIError:
            return None

        # 优先精确的 CJK 匹配，其次归一化匹配
        for f in files:
            fname = f.get("file_name", "")
            fc = cjk_only(fname)
            if target_cjk and len(target_cjk) >= 3 and (
                target_cjk in fc or fc in target_cjk
            ):
                return {"fid": f["fid"], "file_name": fname, "dir": f.get("dir")}
        for f in files:
            fname = f.get("file_name", "")
            fn = normalize(fname)
            if target_norm and len(target_norm) >= 4 and (
                target_norm in fn or fn in target_norm
            ):
                return {"fid": f["fid"], "file_name": fname, "dir": f.get("dir")}
        return None

    async def delete_files(self, fids: list[str]) -> bool:
        """删除自己网盘中的文件/文件夹（回收站）"""
        url = f"{API_DRIVE}/1/clouddrive/file/delete?{COMMON_PARAMS}"
        payload = {"action": 2, "filelist": fids, "exclude_fids": []}
        data = await self._request("POST", url, json=payload)
        if data.get("code") != 0:
            raise QuarkAPIError(f"删除失败: {data.get('message')}")
        # 删除也是异步任务
        task_id = data.get("data", {}).get("task_id")
        if task_id:
            try:
                await self.wait_task(task_id, timeout=20)
            except QuarkAPIError:
                pass  # 删除任务超时不影响结果
        return True

    # ================================================================
    #  5. 生成分享链接
    # ================================================================

    async def find_new_share_since(self, since_ms: int) -> dict | None:
        """
        在我的分享列表中查找 since_ms（毫秒时间戳）之后创建的分享。
        用于 create_share 遇到 401/异常时恢复实际已创建的分享。
        （夸克分享标题使用转存文件夹名，与传入 title 不一致，故按时间匹配）
        """
        url = f"{API_DRIVE}/1/clouddrive/share/mypage/detail"
        params = {
            "pr": "ucpro", "fr": "pc",
            "_page": "1", "_size": "100",
            "fetch_total": "1", "share_all": "1",
        }
        try:
            data = await self._request("GET", url, params=params)
            best = None
            for sh in data.get("data", {}).get("list", []):
                created = sh.get("created_at") or 0
                if created >= since_ms and sh.get("status") == 1:
                    if best is None or created > best["created_at"]:
                        # 优先用接口返回的短链（pwd_id，12位），share_id 是32位内部ID
                        url = sh.get("share_url") or f"{SHARE_PAGE}/s/{sh.get('pwd_id', sh.get('share_id'))}"
                        best = {
                            "url": url,
                            "passcode": sh.get("passcode", "") or "",
                            "share_id": sh.get("share_id", ""),
                            "created_at": created,
                        }
            return best
        except Exception as e:
            logger.warning(f"[QuarkAPI] 查询分享列表失败: {e}")
        return None

    async def list_my_shares(self) -> list[dict]:
        """列出我的分享（前 100 条）"""
        url = f"{API_DRIVE}/1/clouddrive/share/mypage/detail"
        params = {
            "pr": "ucpro", "fr": "pc",
            "_page": "1", "_size": "100",
            "fetch_total": "1", "share_all": "1",
        }
        data = await self._request("GET", url, params=params)
        return data.get("data", {}).get("list", [])

    async def find_share_by_name(self, name: str) -> dict | None:
        """
        按影片名模糊匹配已有分享（分享标题 = 转存文件夹名）。
        匹配规则同 find_existing_file。
        返回前会校验分享链接有效性（排除指向已删除文件的死分享）。
        """
        import re

        def normalize(s: str) -> str:
            s = re.sub(r"\(\d+\)", "", s)
            s = re.sub(r"[\s（）()·：:【】\[\]]", "", s)
            return s.lower()

        target = normalize(name)
        try:
            shares = await self.list_my_shares()
        except Exception:
            return None

        candidates = []
        for sh in shares:
            if sh.get("status") != 1:
                continue
            title = sh.get("title") or ""
            if target and (target in normalize(title) or normalize(title) in target):
                candidates.append(sh)

        # 逐个校验有效性，返回第一个活着的分享
        for sh in candidates:
            share_id = sh.get("share_id", "")
            try:
                await self.get_stoken(share_id)  # 链接失效会抛异常
                short = sh.get("share_url") or f"{SHARE_PAGE}/s/{sh.get('pwd_id', share_id)}"
                return {
                    "url": short,
                    "passcode": sh.get("passcode", "") or "",
                    "share_id": share_id,
                }
            except QuarkAPIError as e:
                logger.debug(f"[QuarkAPI] 分享 {share_id} 已失效({e})，跳过")
                continue
        return None

    async def create_share(
        self, fid_list: list[str], title: str = "", passcode: str = ""
    ) -> dict:
        """
        为自己的文件生成分享链接。
        返回 {"url": "https://pan.quark.cn/s/xxx", "passcode": "xxxx", "share_id": "..."}
        """
        url = f"{API_DRIVE}/1/clouddrive/share"
        payload = {
            "fid_list": fid_list,
            "title": title,
            "url_type": 1,        # 1 = 公开/提取码链接
            "expired_type": 1,    # 永久有效
            "passcode": passcode,
        }
        data = await self._request("POST", url, json=payload)

        if data.get("code") != 0:
            raise QuarkAPIError(
                f"创建分享失败: {data.get('message')}",
                code=data.get("code", -1),
            )

        resp_data = data.get("data", {}) or {}
        logger.debug(f"[QuarkAPI] 创建分享原始响应: {json.dumps(resp_data, ensure_ascii=False)[:300]}")

        share_id = resp_data.get("share_id", "")

        # 有些响应是异步任务，需要轮询获取 share_id
        if not share_id and resp_data.get("task_id"):
            share_id = await self._wait_share_task(resp_data["task_id"])

        if not share_id:
            raise QuarkAPIError(f"创建分享未返回 share_id: {resp_data}")

        pwd = resp_data.get("passcode", "")
        result = {
            "url": f"{SHARE_PAGE}/s/{share_id}",
            "passcode": pwd,
            "share_id": share_id,
        }
        logger.info(f"[QuarkAPI] 分享链接已创建: {result['url']}")
        return result

    async def _wait_share_task(self, task_id: str, timeout: int = 30) -> str:
        """轮询分享创建任务，返回 share_id"""
        url = f"{API_DRIVE}/1/clouddrive/task"
        start = time.time()
        retry_index = 0

        while time.time() - start < timeout:
            params = {
                "pr": "ucpro", "fr": "pc",
                "task_id": task_id, "retry_index": retry_index,
            }
            data = await self._request("GET", url, params=params)
            retry_index += 1

            task_data = data.get("data", {})
            if task_data.get("status") == 2:
                share_id = task_data.get("share_id", "")
                if share_id:
                    return share_id
            await asyncio.sleep(1)

        raise QuarkAPIError("分享任务超时")

    # ================================================================
    #  6. 登录状态检查
    # ================================================================

    async def check_login(self) -> bool:
        """检查 Cookie 是否有效"""
        url = (
            f"{API_DRIVE}/1/clouddrive/file/sort?{COMMON_PARAMS}"
            f"&pdir_fid=0&_page=1&_size=1&_fetch_total=0"
        )
        try:
            data = await self._request("GET", url)
            return data.get("code") == 0
        except Exception:
            return False

    # ================================================================
    #  高层封装：一键转存 + 分享
    # ================================================================

    async def transfer_and_share(
        self,
        share_url: str,
        passcode: str,
        target_folder_fid: str,
        title: str = "",
        skip_share: bool = False,
    ) -> dict:
        """
        一键完成：解析链接 → 转存 → 生成自己的分享链接

        Args:
            share_url:  别人的分享链接 https://pan.quark.cn/s/xxxx
            passcode:   分享提取码（可为空）
            target_folder_fid: 转存目标文件夹 fid
            title:      分享标题
            skip_share: True 时只转存不分享（分享配额受限时使用）

        Returns:
            {"my_url": ..., "my_pwd": ..., "saved_fids": [...]}
        """
        # 解析 pwd_id
        m = re.search(r'pan\.quark\.cn/s/([a-zA-Z0-9]+)', share_url)
        if not m:
            raise QuarkAPIError(f"无法解析分享链接: {share_url}")
        pwd_id = m.group(1)

        # 1. 获取 stoken（链接失效/提取码错误会在此抛异常）
        stoken = await self.get_stoken(pwd_id, passcode)

        # 2. 列出分享文件
        files = await self.list_share_files(pwd_id, stoken)
        if not files:
            raise QuarkAPIError("分享内没有文件")

        fid_list = [f["fid"] for f in files]
        fid_token_list = [f.get("share_fid_token", "") for f in files]

        # 3. 转存
        saved_fids = await self.save_files(
            pwd_id, stoken, fid_list, fid_token_list, target_folder_fid
        )

        if skip_share:
            return {"my_url": "", "my_pwd": "", "saved_fids": saved_fids}

        # 4. 生成自己的分享（失败时尝试从分享列表恢复）
        import time as _time
        # 减 10s 余量：本地时钟与夸克服务器可能有偏差
        before_ms = int(_time.time() * 1000) - 10000
        try:
            share = await self.create_share(saved_fids, title=title)
        except QuarkAPIError as e:
            logger.warning(f"[QuarkAPI] 创建分享异常({e})，尝试从分享列表恢复...")
            await asyncio.sleep(2)
            share = await self.find_new_share_since(before_ms)
            if not share:
                raise QuarkAPIError(
                    f"创建分享失败且无法恢复: {e}",
                    after_transfer=True,
                    saved_fids=saved_fids,
                )

        return {
            "my_url": share["url"],
            "my_pwd": share.get("passcode", ""),
            "saved_fids": saved_fids,
        }
