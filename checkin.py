import json
import os
import logging
import hashlib
import random
import sys
import time
from enum import Enum
from typing import Dict, List, Optional, Tuple, Union
from dataclasses import dataclass, asdict
from pypushdeer import PushDeer
from logging_config import init_logger

try:
    # curl_cffi 复刻浏览器 TLS/HTTP2 指纹，Cloudflare Bot Management 会校验该指纹
    from curl_cffi import requests as http_client
except ImportError as e:  # pragma: no cover - 依赖缺失时的友好提示
    raise SystemExit("缺少依赖 curl_cffi，请先执行 `pip install curl_cffi`") from e


def _fmt_points(value: Union[int, float, str, None]) -> str:
    """格式化积分数值，保留 Team 套餐的 .5 小数"""
    if value is None:
        return "0"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(num)) if num == int(num) else f"{num:.1f}"


class CheckinStatus(Enum):
    """签到状态"""

    SUCCESS = 0
    REPEAT = 1
    DEVICE_MISMATCH = 4  # 自动化签到拦截（设备不匹配）
    TURNSTILE_FAILED = 5  # Turnstile 人机校验未通过（服务端返回 reason: turnstile）
    FAILURE = -2


class ExchangePlan(Enum):
    """兑换计划"""

    PLAN100 = "plan100"
    PLAN200 = "plan200"
    PLAN500 = "plan500"


class APIEndpoint(Enum):
    """API端点"""

    CHECKIN = "/api/user/checkin"
    CHALLENGE = "/api/user/checkin/challenge"
    STATUS = "/api/user/status"
    POINTS = "/api/user/points"
    EXCHANGE = "/api/user/exchange"


class ChallengeConfig:
    """签到挑战协议参数（与线上前端保持一致）"""

    """proof 盐值，前端以字符码数组混淆存储，此处按实测值直接给出"""
    SALT = "glados-observe-v1"

    """proof 拼接符（实测为 "."，非文档所写的 ":"）"""
    SEPARATOR = "."

    """前端强制的最短观测间隔（毫秒）"""
    MIN_OBSERVE_MS = 2500

    """实际等待区间（秒），略长于下限并带抖动，避免固定时序特征"""
    WAIT_RANGE = (2.8, 4.0)


class VIPLevel:
    """会员等级（与官网前端 w={0:"Free",10:"Free",11:"Edu",21:"Basic",31:"Pro",41:"Team",51:"Enterprise"} 一致）"""

    NAMES = {
        0: "Free",
        10: "Free",
        11: "Edu",
        21: "Basic",
        31: "Pro",
        41: "Team",
        51: "Enterprise",
    }

    @classmethod
    def name_of(cls, vip: Union[int, str, None]) -> str:
        try:
            return cls.NAMES.get(int(vip), "Unknown")
        except (TypeError, ValueError):
            return "Unknown"


class APIPlatform:
    """浏览器指纹平台（用于规避设备不匹配拦截）

    不手写 UA：UA 与 sec-ch-ua / TLS 指纹必须自洽，由 curl_cffi 的 impersonate
    配置统一给出。safari 配置本身不发送 sec-ch-ua 与 Upgrade-Insecure-Requests，
    天然符合 XHR 请求形态；chrome 配置为 Windows 平台，切换平台时仍保持自洽。
    """

    MACOS = "macos"
    WINDOWS = "windows"

    IMPERSONATE_MAP = {
        MACOS: "safari",
        WINDOWS: "chrome",
    }

    @classmethod
    def impersonate_of(cls, platform: str) -> str:
        return cls.IMPERSONATE_MAP.get(platform, cls.IMPERSONATE_MAP[cls.MACOS])


class LogEmoji:
    """日志 Emoji 常量"""

    SUCCESS = "✅"
    FAIL = "❌"
    REPEAT = "🔄"
    PENDING = "⏳"
    CHECKIN = "🎫"
    STATUS = "📊"
    POINTS = "💰"
    EXCHANGE = "🎁"
    START = "🚀"
    END = "🏁"
    COOKIE = "🍪"
    DOMAIN = "🌐"
    WARNING = "⚠️ "
    ERROR = "🔴"
    INFO = "ℹ️ "


def log_method(func):
    """日志装饰器"""

    def wrapper(self, *args, **kwargs):
        method_name = func.__name__
        emoji_map = {
            "checkin": LogEmoji.CHECKIN,
            "get_status": LogEmoji.STATUS,
            "get_points": LogEmoji.POINTS,
            "exchange": LogEmoji.EXCHANGE,
        }
        emoji = emoji_map.get(method_name, LogEmoji.INFO)
        try:
            result = func(self, *args, **kwargs)
            return result
        except Exception as e:
            logger.error(f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {LogEmoji.ERROR} {method_name} 执行失败: {e}")

            DEFAULT_ERRORS = {
                "checkin": {"status": "签到失败", "points": "0", "message": ""},
                "get_status": ("None 天", -2),
                "get_points": ("None 积分", 0),
                "exchange": "",
            }

            if method_name in DEFAULT_ERRORS:
                error_template = DEFAULT_ERRORS[method_name]
                if isinstance(error_template, dict):
                    error_result = error_template.copy()
                    error_result["message"] = f"执行失败: {e}"
                    return error_result
                return error_template
            raise

    return wrapper


class Config:
    """应用配置"""

    ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
    ENV_COOKIES = "GLADOS_COOKIES"
    ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
    ENV_VERBOSE = "GLADOS_VERBOSE"

    """默认兑换计划（none 表示不自动兑换）"""
    DEFAULT_EXCHANGE_PLAN = "none"

    """默认是否输出详细响应"""
    DEFAULT_VERBOSE = False

    """默认域名"""
    DOMAINS = ["glados.one"]

    """兑换计划列表"""
    EXCHANGE_PLANS = {
        ExchangePlan.PLAN100.value: 100,
        ExchangePlan.PLAN200.value: 200,
        ExchangePlan.PLAN500.value: 500,
    }

    def __init__(self):
        self.push_key: str = ""
        self.cookies_list: List[str] = []
        self.exchange_plan: str = self.DEFAULT_EXCHANGE_PLAN
        self.verbose: bool = self.DEFAULT_VERBOSE
        self._load_config()

    def _load_config(self) -> None:
        """加载配置"""
        push_key_env: Optional[str] = os.environ.get(self.ENV_PUSH_KEY)
        raw_cookies_env: Optional[str] = os.environ.get(self.ENV_COOKIES)
        exchange_plan_env: Optional[str] = os.environ.get(self.ENV_EXCHANGE_PLAN)
        verbose_env: Optional[str] = os.environ.get(self.ENV_VERBOSE)

        if not push_key_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_PUSH_KEY}' 未设置。")
            self.push_key = ""
        else:
            self.push_key = push_key_env

        if not raw_cookies_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_COOKIES}' 未设置。")
            self.cookies_list = []
        else:
            self.cookies_list = [cookie.strip() for cookie in raw_cookies_env.split("&") if cookie.strip()]
            if not self.cookies_list:
                raise ValueError(f"环境变量 '{self.ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

        if not exchange_plan_env:
            logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 未设置，将使用默认设置（不自动兑换）。")
            self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN
        else:
            if exchange_plan_env in self.EXCHANGE_PLANS:
                self.exchange_plan = exchange_plan_env
                logger.info(f"{LogEmoji.SUCCESS} 使用指定的兑换计划: {self.exchange_plan}")
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将跳过自动兑换。")
                self.exchange_plan = self.DEFAULT_EXCHANGE_PLAN

        logger.info(f"{LogEmoji.INFO} 共加载了 {len(self.cookies_list)} 个 Cookie 用于签到。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}。")
        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_EXCHANGE_PLAN}: {self.exchange_plan}。")

        if verbose_env is not None:
            verbose_env_lower = verbose_env.lower()
            if verbose_env_lower in ["true", "1", "yes", "y"]:
                self.verbose = True
            elif verbose_env_lower in ["false", "0", "no", "n"]:
                self.verbose = False
            else:
                logger.warning(f"{LogEmoji.WARNING} 环境变量 '{self.ENV_VERBOSE}' 的值 '{verbose_env}' 无效，将使用默认值 {self.DEFAULT_VERBOSE}。")

        logger.info(f"{LogEmoji.INFO} 当前 {self.ENV_VERBOSE}: {self.verbose}。")


class API:
    """API 调用"""

    CHECKIN_URL = APIEndpoint.CHECKIN.value
    CHALLENGE_URL = APIEndpoint.CHALLENGE.value
    STATUS_URL = APIEndpoint.STATUS.value
    POINTS_URL = APIEndpoint.POINTS.value
    EXCHANGE_URL = APIEndpoint.EXCHANGE.value

    def __init__(self, domain: str, cookie_index: int = 0, verbose: bool = False, platform: str = APIPlatform.MACOS):
        self.domain: str = domain
        self.cookie_index: int = cookie_index
        self.verbose: bool = verbose
        self.platform: str = platform
        self.headers: Dict[str, str] = self._get_headers()
        self.session = http_client.Session(impersonate=APIPlatform.impersonate_of(self.platform))
        self.session.headers.update(self.headers)

    def __del__(self):
        """关闭 session"""
        self.close()

    def close(self) -> None:
        """关闭 session"""
        if hasattr(self, "session"):
            try:
                self.session.close()
            except Exception as e:
                logger.error(f"{LogEmoji.ERROR} 关闭 session 时发生错误: {e}")

    def __enter__(self):
        """进入上下文管理器"""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """退出上下文管理器"""
        self.close()
        return False

    def _get_headers(self) -> Dict[str, str]:
        """获取请求头

        UA / sec-ch-ua / sec-fetch-* / accept-encoding 由 impersonate 配置注入，
        这里只覆盖接口请求需要与浏览器保持一致的部分。
        """
        return {
            "origin": f"https://{self.domain}",
            "referer": f"https://{self.domain}/console/checkin",
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
            "priority": "u=1, i",
            # 键名带 ";" 是 libcurl 语义：移除该头。chrome 指纹配置默认会带这几个
            # 文档导航才有的头，XHR 请求带着反而异常，这里显式剔除。
            "upgrade-insecure-requests;": "",
            "sec-fetch-user;": "",
            "cache-control;": "",
        }

    def _log(self, level: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        log_message = f"{LogEmoji.COOKIE}[{self.cookie_index}] {LogEmoji.DOMAIN}[{self.domain}] {emoji} {message}"

        if force or self.verbose:
            if level == "info":
                logger.info(log_message)
            elif level == "warning":
                logger.warning(log_message)
            elif level == "error":
                logger.error(log_message)

    def _get_full_url(self, path: str) -> str:
        """获取完整 URL"""
        return f"https://{self.domain}{path}"

    def _make_request(self, url: str, method: str, data: Optional[Dict] = None, cookies: str = "") -> Optional[http_client.Response]:
        """发送 HTTP 请求"""
        session_headers = self.headers.copy()
        session_headers["cookie"] = cookies

        try:
            if method.upper() == "POST":
                response = self.session.post(url, headers=session_headers, data=data, timeout=(60, 120))
            elif method.upper() == "GET":
                response = self.session.get(url, headers=session_headers, timeout=(60, 120))
            else:
                self._log("error", LogEmoji.ERROR, f"不支持的 HTTP 方法: {method}", force=True)
                return None

            if not response.ok:
                self._log("warning", LogEmoji.WARNING, f"向 {url} 发起的请求失败，状态码 {response.status_code}。响应内容: {response.text}", force=True)
                return None
            return response
        except http_client.exceptions.RequestException as e:
            self._log("error", LogEmoji.ERROR, f"向 {url} 发起请求时发生网络错误: {e}", force=True)
            return None

    def _get_checkin_data(self) -> Dict[str, str]:
        """获取签到数据"""
        return {"token": self.domain}

    @staticmethod
    def compute_checkin_proof(nonce: str) -> str:
        """计算签到 proof: SHA-256(nonce + "." + salt) 的 64 位小写十六进制"""
        payload = f"{nonce}{ChallengeConfig.SEPARATOR}{ChallengeConfig.SALT}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_challenge(self, cookies: str) -> Optional[Dict[str, Union[str, int, float, None]]]:
        """获取签到挑战 nonce、ttl 及 Turnstile 要求"""
        url = self._get_full_url(self.CHALLENGE_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if not response:
            return None

        try:
            data = response.json()
        except ValueError:
            self._log("error", LogEmoji.ERROR, "解析挑战响应失败（非 JSON）", force=True)
            return None

        code = data.get("code", -2)
        nonce = data.get("nonce")

        if code != 0 or not nonce:
            self._log(
                "warning",
                LogEmoji.WARNING,
                f"获取挑战失败 {{ code : {code}, message : {data.get('message', '')} }}",
                force=True,
            )
            return None

        ttl = data.get("ttl") or 300000
        # 服务端按风控结果决定是否下发 Turnstile sitekey；下发则签到必须携带 token
        turnstile = data.get("turnstile") or None
        self._log(
            "info",
            LogEmoji.PENDING,
            f"已获取挑战 nonce（ttl={ttl}ms, turnstile={'required' if turnstile else 'off'}）",
        )
        return {"nonce": nonce, "ttl": ttl, "turnstile": turnstile, "at": time.monotonic()}

    def solve_checkin_challenge(self, cookies: str) -> Optional[Dict[str, str]]:
        """完整挑战流程：取 nonce -> 拟人停顿 -> 计算 proof"""
        challenge = self._load_challenge(cookies)
        if not challenge:
            return None

        # 模拟前端 2.5s 强制观测停顿，并加入抖动避免固定时序特征
        wait = random.uniform(*ChallengeConfig.WAIT_RANGE)
        elapsed_ms = (time.monotonic() - challenge["at"]) * 1000
        if elapsed_ms < ChallengeConfig.MIN_OBSERVE_MS:
            wait = max(wait, (ChallengeConfig.MIN_OBSERVE_MS - elapsed_ms) / 1000 + 0.3)

        self._log("info", LogEmoji.PENDING, f"观测停顿 {wait:.2f}s 后提交")
        time.sleep(wait)

        nonce = str(challenge["nonce"])
        proof = self.compute_checkin_proof(nonce)
        self._log("info", LogEmoji.PENDING, f"已计算 proof: {proof[:16]}…")

        # 前端无论是否需要 Turnstile 都会提交 tsState；无 Turnstile 时固定为 off
        payload = {"nonce": nonce, "proof": proof, "tsState": "off"}

        if challenge.get("turnstile"):
            # Turnstile token 需由真实浏览器执行 JS 取得，curl_cffi 无法伪造；
            # 按前端失败分支的语义以 tsState=error 提交，由服务端最终裁决
            payload["tsState"] = "error"
            self._log(
                "warning",
                LogEmoji.WARNING,
                "服务端下发 Turnstile 人机校验，脚本无浏览器会话无法取得 token，本次提交大概率被拒绝",
                force=True,
            )

        return payload

    @log_method
    def checkin(self, cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行签到（完整挑战协议）"""
        url = self._get_full_url(self.CHECKIN_URL)
        checkin_data = self._get_checkin_data()

        result = self._checkin_with_challenge(url, checkin_data, cookies)

        # 网络异常（含超时）：nonce 可能已失效，重新申请后重试一次
        if result["code"] == CheckinStatus.FAILURE and result.get("message") == "网络请求失败":
            self._log("warning", LogEmoji.WARNING, "签到请求网络异常，重新申请 nonce 后重试一次", force=True)
            result = self._checkin_with_challenge(url, checkin_data, cookies)

        # Turnstile 人机校验未通过：token 只能由真实浏览器执行 JS 取得，
        # 重新申请 nonce 解决不了，直接给出明确失败原因
        if result["code"] == CheckinStatus.TURNSTILE_FAILED:
            self._log(
                "error",
                LogEmoji.FAIL,
                f"Turnstile 人机校验未通过 (message: {result.get('message', '')})，"
                "需要真实浏览器会话才能取得 token，跳过重试",
                force=True,
            )
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["points"] = "0"
            result["message"] = f"Turnstile 校验未通过: {result.get('message', '')}"

        # code 4: 检测到自动化签到（设备不匹配），切换指纹平台重试一次
        if result["code"] == CheckinStatus.DEVICE_MISMATCH:
            self._log(
                "warning",
                LogEmoji.WARNING,
                f"检测到自动化签到拦截 (code 4, reason: {result.get('reason', '')})，"
                f"当前平台 {self.platform}，切换到另一平台重试",
                force=True,
            )
            original_platform = self.platform
            alt_platform = (
                APIPlatform.WINDOWS if self.platform == APIPlatform.MACOS else APIPlatform.MACOS
            )
            self._switch_platform(alt_platform)
            self._log("info", LogEmoji.PENDING, f"切换指纹平台为 {alt_platform} 后重试签到", force=True)
            result = self._checkin_with_challenge(url, checkin_data, cookies)
            # 无论重试结果如何，恢复原平台
            self._switch_platform(original_platform)

            # 重试后若仍被拦截（code 4），视为失败
            if result["code"] == CheckinStatus.DEVICE_MISMATCH:
                self._log(
                    "error",
                    LogEmoji.FAIL,
                    "切换指纹平台后仍被拦截 (code 4)，签到失败",
                    force=True,
                )
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
                result["message"] = f"重试后仍被拦截: {result.get('message', '')}"

        return result

    def _switch_platform(self, platform: str) -> None:
        """切换浏览器指纹平台（impersonate 绑定在 Session 上，需重建 Session）"""
        self.platform = platform
        self.headers = self._get_headers()
        self.close()
        self.session = http_client.Session(impersonate=APIPlatform.impersonate_of(platform))
        self.session.headers.update(self.headers)

    def _checkin_with_challenge(self, url: str, base_data: Dict[str, str], cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """申请挑战 -> 计算 proof -> 提交签到"""
        challenge = self.solve_checkin_challenge(cookies)
        if not challenge:
            return {
                "status": "签到失败",
                "points": "0",
                "message": "挑战获取失败",
                "code": CheckinStatus.FAILURE,
            }

        checkin_data = {**base_data, **challenge}
        return self._do_checkin(url, checkin_data, cookies)

    def _do_checkin(self, url: str, checkin_data: Dict[str, str], cookies: str) -> Dict[str, Union[str, CheckinStatus]]:
        """执行一次签到请求"""
        response = self._make_request(url, "POST", checkin_data, cookies)

        result = {
            "status": "签到失败",
            "points": "0",
            "message": "",
            "streak": None,
            "points_total": None,
            "code": CheckinStatus.FAILURE,
        }

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "无消息字段")
            reason = data.get("reason", "")
            points = data.get("points", 0)
            streak = data.get("streak")
            history = data.get("list") or []
            balance = None
            if history and isinstance(history[0], dict):
                balance = history[0].get("balance")

            if code == CheckinStatus.SUCCESS.value:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {points}, message : {message} }}")
                result["code"] = CheckinStatus.SUCCESS
                result["status"] = "签到成功"
                result["points"] = _fmt_points(points)
                result["message"] = message
                result["streak"] = streak
                result["points_total"] = _fmt_points(balance) if balance is not None else None
            elif code == CheckinStatus.REPEAT.value:
                self._log("info", LogEmoji.REPEAT, f"{{ code : {code}, message : {message} }}", force=True)
                result["code"] = CheckinStatus.REPEAT
                result["status"] = "重复签到"
                result["points"] = "0"
                result["message"] = message
                result["streak"] = streak
                result["points_total"] = _fmt_points(balance) if balance is not None else None
            elif code == CheckinStatus.DEVICE_MISMATCH.value:
                self._log(
                    "info",
                    LogEmoji.WARNING,
                    f"{{ code : {code}, reason : {reason}, message : {message} }}",
                    force=True,
                )
                result["code"] = CheckinStatus.DEVICE_MISMATCH
                result["status"] = "检测到自动化签到"
                result["points"] = "0"
                result["message"] = message
                result["reason"] = reason
            elif code == CheckinStatus.TURNSTILE_FAILED.value or reason == "turnstile":
                # 前端以 reason: "turnstile" 判定人机校验失败，code 值可能变化
                self._log(
                    "info",
                    LogEmoji.WARNING,
                    f"{{ code : {code}, reason : {reason}, message : {message} }}",
                    force=True,
                )
                result["code"] = CheckinStatus.TURNSTILE_FAILED
                result["status"] = "Turnstile 校验未通过"
                result["points"] = "0"
                result["message"] = message
                result["reason"] = reason
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                result["code"] = CheckinStatus.FAILURE
                result["status"] = "签到失败"
                result["points"] = "0"
                result["message"] = message
        else:
            self._log("warning", LogEmoji.WARNING, "签到失败", force=True)
            result["code"] = CheckinStatus.FAILURE
            result["status"] = "签到失败"
            result["message"] = "网络请求失败"

        return result

    @log_method
    def get_status(self, cookies: str) -> Tuple[str, int, str]:
        """获取状态（剩余天数 + 响应码 + 会员等级）"""

        url = self._get_full_url(self.STATUS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            payload = data.get("data") or {}
            left_days = payload.get("leftDays")
            vip_name = VIPLevel.name_of(payload.get("vip"))

            if code == -101:
                self._log("error", LogEmoji.ERROR, "账号已过期 (code -101)", force=True)
                return "已过期", code, vip_name

            if left_days is not None:
                left_days_int = int(float(left_days))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, leftDays : {left_days_int} 天, vip : {vip_name}}}")
                return f"{left_days_int} 天", code, vip_name
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, leftDays : {left_days} 天}}", force=True)
                return "None 天", code, vip_name
        else:
            self._log("warning", LogEmoji.WARNING, "获取状态失败", force=True)
            return "None 天", -2, "Unknown"

    @log_method
    def get_points(self, cookies: str) -> Tuple[str, int, Optional[int]]:
        """获取积分（总积分 + 数值 + 连签天数）"""
        url = self._get_full_url(self.POINTS_URL)
        response = self._make_request(url, "GET", cookies=cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            points = data.get("points", None)
            streak = data.get("streak")

            if points is not None:
                points_str = f"{_fmt_points(points)} 积分"
                points_num = int(float(points))
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, points : {_fmt_points(points)} 积分, streak : {streak}}}")
                return points_str, points_num, streak
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, points : {points} 积分}}", force=True)
                return "None 积分", 0, streak
        else:
            self._log("warning", LogEmoji.WARNING, "获取积分失败", force=True)
            return "None 积分", 0, None

    @log_method
    def exchange(self, cookies: str, plan: str, required_points: int) -> str:
        """执行兑换"""
        url = self._get_full_url(self.EXCHANGE_URL)
        response = self._make_request(url, "POST", {"planType": plan}, cookies)

        if response:
            data = response.json()
            code = data.get("code", -2)
            message = data.get("message", "未知错误")

            if code == 0:
                self._log("info", LogEmoji.SUCCESS, f"{{ code : {code}, message : {message} }}")
                return f"兑换成功: {plan}"
            else:
                self._log("info", LogEmoji.FAIL, f"{{ code : {code}, message : {message} }}", force=True)
                return f"兑换失败: {message}"
        else:
            self._log("warning", LogEmoji.WARNING, "兑换失败", force=True)
            return "兑换失败"


@dataclass()
class CheckinResult:
    """签到结果"""

    cookie_index: int
    domain: str
    status: str = "签到失败"
    points: str = "0"
    days: str = "None"
    points_total: str = "None"
    streak: str = "-"
    vip: str = "Unknown"
    exchange: str = "未兑换"
    code: CheckinStatus = CheckinStatus.FAILURE  # 0: 成功, 1: 重复, -2: 失败

    def to_dict(self) -> Dict[str, Union[str, CheckinStatus]]:
        result_dict = asdict(self)
        return result_dict


class PushService:
    """推送服务"""

    def __init__(self, config: Config):
        self.config = config

    def send(self, title: str, content: str) -> bool:
        """发送推送"""
        if not self.config.push_key:
            logger.info(f"{LogEmoji.WARNING} 未设置推送密钥，跳过推送通知。")
            return False

        try:
            pushdeer = PushDeer(pushkey=self.config.push_key)
            pushdeer.send_text(title, desp=content)
            logger.info(f"{LogEmoji.SUCCESS} 推送通知发送成功。")
            return True
        except Exception as e:
            logger.error(f"{LogEmoji.ERROR} 发送推送通知失败: {e}")
            return False


class Checker:
    """签到"""

    def __init__(self, config: Config):
        self.config = config
        self.results = []

    def _log(self, cookie_idx: int, domain: str, emoji: str, message: str, force: bool = False) -> None:
        """统一日志输出方法"""

        if self.config.verbose or force:
            logger.info(f"{LogEmoji.COOKIE}[{cookie_idx}] {LogEmoji.DOMAIN}[{domain}] {emoji} {message}")

    def checkin_all(self):
        """执行所有签到任务"""
        cookie_count = len(self.config.cookies_list)
        domain_count = len(self.config.DOMAINS)
        total_tasks = cookie_count * domain_count
        task_idx = 0

        logger.info(f"{LogEmoji.INFO} 共 {cookie_count} 个 Cookie, {domain_count} 个域名, 共 {total_tasks} 个任务")

        for cookie_idx, cookie in enumerate(self.config.cookies_list, 1):
            logger.info(f"{LogEmoji.START} ========== 开始处理 Cookie {cookie_idx} ==========")

            for domain in self.config.DOMAINS:
                task_idx += 1
                logger.info(f"{LogEmoji.INFO} ----- 任务 {task_idx}/{total_tasks}: {LogEmoji.COOKIE}[{cookie_idx}] on {LogEmoji.DOMAIN}[{domain}] -----")

                result = self._checkin_on_domain(cookie, cookie_idx, domain)
                self.results.append(result)

                result_message = f"结果: {result.status}"
                if result.code == CheckinStatus.SUCCESS:
                    if self.config.verbose:
                        result_message = f"结果: {result.status}, 获得 {result.points} 积分, 剩余 {result.days}, 总 {result.points_total}, {result.exchange}"
                    self._log(cookie_idx, domain, LogEmoji.SUCCESS, result_message, force=True)
                else:
                    self._log(cookie_idx, domain, LogEmoji.WARNING, result_message, force=True)

    def _checkin_on_domain(self, cookie: str, cookie_idx: int, domain: str) -> CheckinResult:
        result = CheckinResult(cookie_idx, domain)

        with API(domain, cookie_idx, verbose=self.config.verbose) as api:
            # 1. 获取状态
            self._log(cookie_idx, domain, LogEmoji.STATUS, "查询剩余天数")
            days_str, status_code, vip_name = api.get_status(cookie)
            result.days = days_str
            result.vip = vip_name

            # 2. 签到（完整挑战协议）
            self._log(cookie_idx, domain, LogEmoji.CHECKIN, "执行签到")
            checkin_result = api.checkin(cookie)
            result.status = checkin_result["status"]
            result.code = checkin_result.get("code", CheckinStatus.FAILURE)
            result.points = str(checkin_result.get("points", "0"))
            if checkin_result.get("streak") is not None:
                result.streak = f"{checkin_result['streak']} 天"

            # 3. 获取积分
            self._log(cookie_idx, domain, LogEmoji.POINTS, "查询总积分")
            points_str, points_num, streak = api.get_points(cookie)
            result.points_total = points_str
            if streak is not None:
                result.streak = f"{streak} 天"
            # 签到响应 list[0].balance 是签到后的即时余额，优先于 points 接口
            if checkin_result.get("points_total") is not None:
                result.points_total = f"{checkin_result['points_total']} 积分"
            elif result.points_total == "None 积分" and points_num:
                result.points_total = f"{points_num} 积分"

            # 4. 执行兑换（未配置有效兑换计划时跳过）
            if self.config.exchange_plan in self.config.EXCHANGE_PLANS:
                required_points = self.config.EXCHANGE_PLANS[self.config.exchange_plan]
                self._log(
                    cookie_idx,
                    domain,
                    LogEmoji.EXCHANGE,
                    f"开始兑换 {self.config.exchange_plan} (需要 {required_points} 积分)",
                )
                result.exchange = api.exchange(cookie, self.config.exchange_plan, required_points)
            else:
                result.exchange = "未配置兑换计划，跳过自动兑换"
                self._log(cookie_idx, domain, LogEmoji.INFO, "未配置兑换计划，跳过自动兑换", force=True)

        return result

    def get_results(self) -> List[Dict[str, str]]:
        """获取所有结果"""
        return [result.to_dict() for result in self.results]

    def format_results(self) -> Tuple[str, str, str]:
        """格式化结果"""
        results = self.get_results()

        success_count = sum(1 for r in results if r["code"] == CheckinStatus.SUCCESS)
        repeat_count = sum(1 for r in results if r["code"] == CheckinStatus.REPEAT)
        device_mismatch_count = sum(1 for r in results if r["code"] == CheckinStatus.DEVICE_MISMATCH)
        fail_count = sum(1 for r in results if r["code"] == CheckinStatus.FAILURE)

        title = f"GLaDOS 签到, 成功{success_count}, 失败{fail_count}, 重复{repeat_count}"
        if device_mismatch_count:
            title += f", 拦截{device_mismatch_count}"

        send_content_lines = []
        log_content_lines = []
        for i, res in enumerate(results, 1):
            line = (
                f"#{i} [{res['vip']}] 本次+{res['points']} 连签:{res['streak']} "
                f"剩余:{res['days']} 总积分:{res['points_total']} | {res['status']} | {res['exchange']}"
            )
            send_content_lines.append(line)

            if self.config.verbose:
                log_line = line
            else:
                log_line = f"#{i} {res['status']}"
            log_content_lines.append(log_line)

        content = "\n".join(send_content_lines)
        log_content = "\n".join(log_content_lines)
        return title, content, log_content


# 初始化日志
logger = init_logger()


def main():
    """主函数"""
    exit_code = 0
    try:
        # 1. 加载配置
        logger.info(f"{LogEmoji.START} 步骤 1: 加载配置")
        config = Config()

        if not config.cookies_list:
            logger.error(f"{LogEmoji.ERROR} 未找到有效的 Cookie, 退出程序。")
            title, content = "# 未找到 cookies!", ""
            exit_code = 1
        else:
            # 2. 执行签到
            logger.info(f"{LogEmoji.START} 步骤 2: 执行签到")
            checker = Checker(config)
            checker.checkin_all()

            # 3. 格式化结果
            logger.info(f"{LogEmoji.START} 步骤 3: 格式化结果")
            title, content, log_content = checker.format_results()
            logger.info(f"\n{LogEmoji.END}========== 签到总结 ==========\n{title}\n{log_content}")
            if any(r["code"] == CheckinStatus.FAILURE for r in checker.get_results()):
                exit_code = 1

    except Exception as e:
        logger.error(f"{LogEmoji.ERROR} 主程序执行过程中发生未预期的错误: {e}")
        title, content, log_content = "# 脚本执行出错", str(e), str(e)
        exit_code = 1

    # 4. 发送推送
    logger.info(f"{LogEmoji.START} 步骤 4: 发送推送")
    push_service = PushService(config if "config" in locals() else "")
    push_service.send(title, content)
    logger.info(f"{LogEmoji.END} 签到完成")

    if exit_code:
        logger.error(f"{LogEmoji.ERROR} 存在失败账号，以非零退出码结束，触发 Actions 告警")
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
