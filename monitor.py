#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Comic-Days 漫画监控脚本

功能:
- 监控多部漫画更新
- 自动下载最新话
- 支持票据/PT购买
- 纯 HTTP API，无需浏览器

使用:
    python monitor.py --config config.yaml
    python monitor.py  # 使用默认配置
"""

import os
import sys
import json
import yaml
import time
import re
import html
import base64
import math
import argparse
import logging
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

try:
    from PIL import Image
except ImportError:
    print("[ERROR] 需要 Pillow: pip install Pillow")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("[ERROR] 需要 PyYAML: pip install PyYAML")
    sys.exit(1)


# ============================================================================
# 全局配置
# ============================================================================

BASE_URL = "https://comic-days.com"
DIVIDE_NUM = 4
MULTIPLE_NUM = 8


def get_base_dir() -> Path:
    """获取基础目录"""
    return Path(os.environ.get("COMIC_DAYS_DIR", Path(__file__).parent))


# ============================================================================
# URL 解析
# ============================================================================

def parse_series_id_from_url(url: str) -> Tuple[Optional[str], bool]:
    """从 URL 中提取 series_id
    
    返回: (series_id, is_episode_url)
    - 如果是 series URL，直接返回 series_id
    - 如果是 episode URL，返回 episode_id 和 True（需要进一步查询 series_id）
    """
    # 匹配 /series/{series_id}
    series_match = re.search(r'/series/(\d+)', url)
    if series_match:
        return series_match.group(1), False
    
    # 匹配 /episode/{episode_id}
    episode_match = re.search(r'/episode/(\d+)', url)
    if episode_match:
        return episode_match.group(1), True
    
    # 纯数字，假设是 series_id
    if url.isdigit():
        return url, False
    
    return None, False


def get_series_id_from_episode(episode_id: str) -> Optional[str]:
    """从 episode 页面提取 series_id"""
    try:
        resp = requests.get(
            f"{BASE_URL}/episode/{episode_id}",
            headers=get_headers(),
            timeout=15
        )
        
        # 方法1: 从 next/prev 链接中找 series
        series_match = re.search(r'/series/(\d+)', resp.text)
        if series_match:
            return series_match.group(1)
        
        # 方法2: 从 JSON 数据中找
        json_match = re.search(r'"series"\s*:\s*\{\s*"databaseId"\s*:\s*"?(\d+)"?', resp.text)
        if json_match:
            return json_match.group(1)
        
    except Exception:
        pass
    
    return None


def normalize_comic_config(comic: dict, logger) -> Optional[dict]:
    """标准化漫画配置，支持 URL 或 series_id"""
    result = {"enabled": comic.get("enabled", True), "name": comic.get("name", "Unknown")}
    
    # 如果已有 series_id，直接用
    if comic.get("series_id"):
        result["series_id"] = comic["series_id"]
        return result
    
    # 如果有 URL，解析
    if comic.get("url"):
        url = comic["url"]
        parsed_id, is_episode = parse_series_id_from_url(url)
        
        if parsed_id is None:
            logger.error(f"Cannot parse URL: {url}")
            return None
        
        if is_episode:
            # 从 episode URL 获取 series_id
            logger.info(f"Extracting series_id from episode {parsed_id}...")
            series_id = get_series_id_from_episode(parsed_id)
            if series_id:
                result["series_id"] = series_id
                logger.info(f"  Found series_id: {series_id}")
            else:
                logger.error(f"Cannot find series_id from episode {parsed_id}")
                return None
        else:
            result["series_id"] = parsed_id
        
        return result
    
    logger.error(f"Comic config missing series_id or url: {comic}")
    return None


# ============================================================================
# 日志配置
# ============================================================================

def setup_logging(config: dict):
    """配置日志"""
    log_config = config.get("logging", {})
    level = getattr(logging, log_config.get("level", "INFO"))
    
    handlers = []
    
    if log_config.get("console", True):
        # Windows 控制台编码修复
        if sys.platform == 'win32':
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        handlers.append(logging.StreamHandler(sys.stdout))
    
    if log_config.get("file"):
        log_file = get_base_dir() / log_config["file"]
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding='utf-8'))
    
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
        handlers=handlers
    )
    
    return logging.getLogger(__name__)


# ============================================================================
# HTTP 工具
# ============================================================================

def get_headers() -> dict:
    """获取通用请求头"""
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }


def safe_request(session: requests.Session, method: str, url: str, max_retries: int = 3, **kwargs) -> requests.Response:
    """带重试的安全请求"""
    for attempt in range(max_retries):
        try:
            if method.upper() == "GET":
                return session.get(url, **kwargs)
            elif method.upper() == "POST":
                return session.post(url, **kwargs)
        except (requests.exceptions.SSLError, requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise
    raise requests.exceptions.RequestException("Max retries exceeded")


# ============================================================================
# API 函数
# ============================================================================

def http_login(session: requests.Session, email: str, password: str) -> bool:
    """HTTP 登录"""
    safe_request(session, "GET", f"{BASE_URL}/", headers=get_headers())
    
    resp = safe_request(
        session, "POST",
        f"{BASE_URL}/user_account/login",
        headers={
            **get_headers(),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        data={"email_address": email, "password": password}
    )
    
    return resp.status_code == 200 and 'glsc' in session.cookies


def http_get_user_info(session: requests.Session) -> dict:
    """获取用户信息"""
    resp = safe_request(session, "GET", f"{BASE_URL}/my.json", headers=get_headers())
    if resp.status_code == 200:
        return resp.json()
    return {}


def http_use_ticket(session: requests.Session, episode_id: str) -> Tuple[bool, str]:
    """使用票据购买"""
    graphql_id = base64.b64encode(f"Episode:{episode_id}".encode()).decode()
    
    query = """
    mutation PurchaseViaTicket($id: ID!) {
      purchaseViaTicket(input: {id: $id}) {
        product { __typename }
      }
    }
    """
    
    resp = safe_request(
        session, "POST",
        f"{BASE_URL}/graphql?opname=Viewer_PurchaseViaTicket",
        headers={**get_headers(), "Content-Type": "application/json"},
        json={"query": query, "variables": {"id": graphql_id}}
    )
    
    try:
        data = resp.json()
        if "errors" in data:
            return False, data['errors'][0].get('extensions', {}).get('code', 'unknown')
        if data.get("data", {}).get("purchaseViaTicket"):
            return True, "OK"
        return False, "unknown"
    except Exception as e:
        return False, str(e)


def http_purchase_pt(session: requests.Session, episode_id: str) -> Tuple[bool, str]:
    """使用 PT 购买"""
    graphql_id = base64.b64encode(f"Episode:{episode_id}".encode()).decode()
    
    query = """
    mutation Purchase($id: ID!) {
      purchase(input: {id: $id}) {
        product { __typename }
      }
    }
    """
    
    resp = safe_request(
        session, "POST",
        f"{BASE_URL}/graphql?opname=Viewer_Purchase",
        headers={**get_headers(), "Content-Type": "application/json"},
        json={"query": query, "variables": {"id": graphql_id}}
    )
    
    try:
        data = resp.json()
        if "errors" in data:
            return False, data['errors'][0].get('extensions', {}).get('code', 'unknown')
        if data.get("data", {}).get("purchase"):
            return True, "OK"
        return False, "unknown"
    except Exception as e:
        return False, str(e)


def fetch_episodes_from_atom(series_id: str) -> List[dict]:
    """从 Atom feed 获取章节列表"""
    url = f"{BASE_URL}/atom/series/{series_id}"
    
    try:
        resp = requests.get(url, headers=get_headers(), timeout=30)
        if resp.status_code != 200:
            return []
        
        episodes = []
        entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)
        
        for entry in entries:
            link_m = re.search(r'comic-days\.com/episode/(\d+)', entry)
            title_m = re.search(r'<title>([^<]+)</title>', entry)
            updated_m = re.search(r'<updated>([^<]+)</updated>', entry)
            published_m = re.search(r'<published>([^<]+)</published>', entry)
            free_m = re.search(r'<giga:freeTermStartDate>([^<]+)</giga:freeTermStartDate>', entry)
            
            if link_m:
                episodes.append({
                    'id': link_m.group(1),
                    'title': html.unescape(title_m.group(1)) if title_m else '',
                    'updated': updated_m.group(1) if updated_m else None,
                    'published': published_m.group(1) if published_m else None,
                    'is_free': bool(free_m)
                })
        
        return episodes
    except Exception as e:
        logging.error(f"[ATOM] Error: {e}")
        return []


def http_get_episode_info(ep_id: str, session: Optional[requests.Session] = None) -> dict:
    """获取章节信息"""
    if session is None:
        session = requests.Session()
    
    resp = safe_request(session, "GET", f"{BASE_URL}/episode/{ep_id}", headers=get_headers(), timeout=15)
    html_content = resp.content.decode('utf-8')
    
    price_m = re.search(r'<span>(?:購入|ログイン)[^<]*</span>\s*<span>(\d+)pt</span>', html_content)
    price = int(price_m.group(1)) if price_m else None
    
    has_ticket = 'data-ticket-rental-id' in html_content
    is_free = not bool(re.search(r'(?:購入して読む|ログインして読む)', html_content))
    
    return {
        'id': ep_id,
        'price': price,
        'has_ticket': has_ticket,
        'is_free': is_free
    }


def http_get_image_urls(session: requests.Session, episode_id: str) -> Tuple[str, List[str]]:
    """获取章节图片 URL"""
    resp = safe_request(session, "GET", f"{BASE_URL}/episode/{episode_id}", headers=get_headers())
    
    urls = re.findall(
        r'https://cdn-img\.comic-days\.com/public/page/\d+/[0-9]+-[a-f0-9]+',
        resp.text
    )
    urls = list(dict.fromkeys(urls))
    
    title_match = re.search(r'"episode_title":\s*"([^"]+)"', resp.text)
    if not title_match:
        title_match = re.search(r'<title>([^<]+)\s*\|', resp.text)
    title = html.unescape(title_match.group(1).strip()) if title_match else f"episode_{episode_id}"
    
    return title, urls


# ============================================================================
# 图片处理
# ============================================================================

def unscramble(img: Image.Image) -> Image.Image:
    """解码打乱的图片"""
    w, h = img.size
    bw = int(math.floor(w / (DIVIDE_NUM * MULTIPLE_NUM)) * MULTIPLE_NUM)
    bh = int(math.floor(h / (DIVIDE_NUM * MULTIPLE_NUM)) * MULTIPLE_NUM)
    result = Image.new(img.mode, (w, h))
    
    for e in range(DIVIDE_NUM * DIVIDE_NUM):
        di = (e % DIVIDE_NUM) * DIVIDE_NUM + (e // DIVIDE_NUM)
        sx, sy = (e % DIVIDE_NUM) * bw, (e // DIVIDE_NUM) * bh
        dx, dy = (di % DIVIDE_NUM) * bw, (di // DIVIDE_NUM) * bh
        result.paste(img.crop((sx, sy, sx + bw, sy + bh)), (dx, dy))
    
    return result


def download_episode(session: requests.Session, episode_id: str, series_name: str, download_dir: Path) -> dict:
    """下载章节"""
    title, urls = http_get_image_urls(session, episode_id)
    
    if not urls:
        return {"success": 0, "total": 0, "title": None}
    
    safe_filename = title
    for c in ['/', '\\', ':', '?', '*', '"', '<', '>', '|', '…']:
        safe_filename = safe_filename.replace(c, '')
    safe_filename = safe_filename.replace('　', ' ')
    
    output_dir = download_dir / series_name / safe_filename
    output_dir.mkdir(parents=True, exist_ok=True)
    
    success = 0
    for i, url in enumerate(urls, 1):
        try:
            r = safe_request(session, "GET", url, timeout=30)
            if r.status_code == 200:
                img = Image.open(BytesIO(r.content))
                unscramble(img).save(output_dir / f'{i:03d}.jpg', 'JPEG', quality=95)
                success += 1
        except Exception as e:
            logging.warning(f"  Image {i} failed: {e}")
    
    logging.info(f"  Downloaded: {title[:30]} ({success}/{len(urls)})")
    
    return {
        "success": success,
        "total": len(urls),
        "title": title,
        "output_dir": str(output_dir)
    }


# ============================================================================
# 状态管理
# ============================================================================

def load_state(series_id: str, state_dir: Path) -> dict:
    """加载作品状态"""
    state_file = state_dir / f"{series_id}.json"
    if state_file.exists():
        try:
            return json.loads(state_file.read_text(encoding='utf-8'))
        except:
            pass
    return {
        "series_id": series_id,
        "last_checked": None,
        "purchased": {},       # {episode_id: {title, mode, ...}}
        "ticket_used": {}      # {email: timestamp}
    }


def save_state(state: dict, state_dir: Path):
    """保存作品状态（原子写入）"""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{state['series_id']}.json"
    
    # 原子写入
    temp_file = state_file.with_suffix('.tmp')
    try:
        temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        if state_file.exists():
            state_file.unlink()
        temp_file.rename(state_file)
    except Exception as e:
        if temp_file.exists():
            temp_file.unlink()
        raise e


def load_accounts(accounts_file: Path) -> List[dict]:
    """加载账号池（包含 PT 余额和可选 Session）"""
    if not accounts_file.exists():
        return []
    
    try:
        data = json.loads(accounts_file.read_text(encoding='utf-8'))
        return data.get('accounts', [])
    except:
        return []


def save_accounts(accounts: List[dict], accounts_file: Path):
    """保存账号池（原子写入）"""
    data = {'accounts': accounts, 'updated_at': datetime.now().isoformat()}
    
    # 原子写入：先写临时文件，再重命名
    temp_file = accounts_file.with_suffix('.tmp')
    try:
        temp_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        # Windows 需要先删除目标文件
        if accounts_file.exists():
            accounts_file.unlink()
        temp_file.rename(accounts_file)
    except Exception as e:
        if temp_file.exists():
            temp_file.unlink()
        raise e


# 全局 Session 缓存（避免频繁登录）
_session_cache: Dict[str, requests.Session] = {}


def get_session(email: str, password: str, force_new: bool = False) -> Optional[requests.Session]:
    """获取或创建 Session（带缓存）"""
    if not force_new and email in _session_cache:
        session = _session_cache[email]
        # 验证 Session 是否有效
        try:
            resp = session.get(f"{BASE_URL}/my.json", headers=get_headers(), timeout=10)
            if resp.status_code == 200 and resp.json().get('logged_in'):
                return session
        except:
            pass
    
    # 需要重新登录
    session = requests.Session()
    if http_login(session, email, password):
        _session_cache[email] = session
        return session
    
    return None


def process_episode(
    episode_id: str,
    series_id: str,
    series_name: str,
    accounts: List[dict],
    accounts_file: Path,
    state: dict,
    download_dir: Path,
    state_dir: Path,
    logger
) -> Tuple[bool, List[dict]]:
    """处理单个章节 - 遍历账号直到买成功
    
    返回: (success, updated_accounts)
    """
    # 检查是否免费
    session = requests.Session()
    ep_info = http_get_episode_info(episode_id, session)
    
    if ep_info['is_free']:
        logger.info(f"  Free episode: {episode_id}")
        result = download_episode(session, episode_id, series_name, download_dir)
        state["purchased"][episode_id] = {
            "title": result.get("title"),
            "mode": "free",
            "downloaded": result["success"],
            "total": result["total"],
            "at": datetime.now().isoformat()
        }
        save_state(state, state_dir)
        return True, accounts
    
    # 需要购买 - 只尝试 PT > 0 的账号
    available_accounts = [acc for acc in accounts if acc.get('pt', 0) > 0]
    logger.info(f"  Trying {len(available_accounts)} accounts with PT > 0...")
    
    for i, account in enumerate(available_accounts):
        email = account['email']
        password = account['password']
        
        # 获取 Session（带缓存，避免频繁登录）
        session = get_session(email, password)
        if session is None:
            logger.info(f"    {email[:25]}... login failed")
            continue
        
        # 获取真实 PT
        user_info = http_get_user_info(session)
        actual_pt = user_info.get("point", {}).get("total", 0)
        
        # 更新账号 PT
        account['pt'] = actual_pt
        
        logger.info(f"    {email[:25]}... PT: {actual_pt}")
        
        price = ep_info.get('price', 0)
        
        # 尝试票据
        ticket_info = state.get("ticket_used", {}).get(email)
        ticket_charged = True
        if ticket_info:
            try:
                last_used = datetime.fromisoformat(ticket_info)
                if datetime.now() - last_used < timedelta(hours=23):
                    ticket_charged = False
            except:
                pass
        
        if ticket_charged:
            success, msg = http_use_ticket(session, episode_id)
            if success:
                state.setdefault("ticket_used", {})[email] = datetime.now().isoformat()
                result = download_episode(session, episode_id, series_name, download_dir)
                state["purchased"][episode_id] = {
                    "title": result.get("title"),
                    "mode": "ticket",
                    "account": email,
                    "downloaded": result["success"],
                    "total": result["total"],
                    "at": datetime.now().isoformat()
                }
                save_state(state, state_dir)
                save_accounts(accounts, accounts_file)
                return True, accounts
        
        # 尝试 PT
        if actual_pt < price:
            logger.info(f"    Insufficient PT: {actual_pt} < {price}")
            continue
        
        success, msg = http_purchase_pt(session, episode_id)
        if success:
            # 更新 PT（扣减）
            account['pt'] = actual_pt - price
            
            result = download_episode(session, episode_id, series_name, download_dir)
            state["purchased"][episode_id] = {
                "title": result.get("title"),
                "mode": "pt",
                "account": email,
                "price": price,
                "downloaded": result["success"],
                "total": result["total"],
                "at": datetime.now().isoformat()
            }
            save_state(state, state_dir)
            save_accounts(accounts, accounts_file)
            return True, accounts
        else:
            logger.warning(f"    PT purchase failed: {msg}")
    
    # 没成功，但保存更新后的 PT
    save_accounts(accounts, accounts_file)
    logger.warning(f"  All accounts failed for {episode_id}")
    return False, accounts


def monitor_series(
    series_id: str,
    series_name: str,
    accounts: List[dict],
    accounts_file: Path,
    config: dict,
    logger
) -> Tuple[int, List[dict]]:
    """监控单部漫画
    
    返回: (success_count, updated_accounts)
    """
    base_dir = get_base_dir()
    download_dir = base_dir / config.get("monitor", {}).get("download_dir", "./downloads")
    state_dir = base_dir / config.get("monitor", {}).get("state_dir", "./states")
    
    # 加载状态
    state = load_state(series_id, state_dir)
    state["series_name"] = series_name
    
    logger.info(f"Checking: {series_name} ({series_id})")
    
    # 获取所有章节
    episodes = fetch_episodes_from_atom(series_id)
    if not episodes:
        logger.warning(f"No episodes found")
        return 0, accounts
    
    # 过滤已下载的
    purchased = state.get("purchased", {})
    new_episodes = [ep for ep in episodes if ep['id'] not in purchased]
    
    if not new_episodes:
        logger.info(f"  No new episodes")
        state["last_checked"] = datetime.now().isoformat()
        save_state(state, state_dir)
        return 0, accounts
    
    logger.info(f"  Found {len(new_episodes)} new episodes")
    
    # 只处理最新的 N 话
    latest_only = config.get("monitor", {}).get("latest_only", 1)
    if latest_only > 0 and len(new_episodes) > latest_only:
        new_episodes = new_episodes[:latest_only]
        logger.info(f"  Processing latest {len(new_episodes)} episodes")
    
    # 处理每个新章节
    success_count = 0
    for ep in new_episodes:
        logger.info(f"  Episode: {ep['id']} - {ep['title'][:30] if ep['title'] else 'N/A'}")
        
        success, accounts = process_episode(
            ep['id'],
            series_id,
            series_name,
            accounts,
            accounts_file,
            state,
            download_dir,
            state_dir,
            logger
        )
        
        if success:
            success_count += 1
    
    state["last_checked"] = datetime.now().isoformat()
    save_state(state, state_dir)
    
    return success_count, accounts


def main():
    """主入口"""
    parser = argparse.ArgumentParser(description="Comic-Days Monitor")
    parser.add_argument("--config", type=str, default="config.yaml", help="配置文件路径")
    parser.add_argument("--series", type=str, help="只检查指定 series_id")
    args = parser.parse_args()
    
    # 加载配置
    config_path = Path(args.config)
    if not config_path.exists():
        config_path = get_base_dir() / "config.yaml"
    
    if config_path.exists():
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    else:
        config = {}
    
    # 配置日志
    logger = setup_logging(config)
    logger.info("=" * 60)
    logger.info("Comic-Days Monitor Started")
    logger.info("=" * 60)
    
    # 路径
    base_dir = get_base_dir()
    accounts_file = base_dir / config.get("accounts", {}).get("file", "./accounts.json")
    
    # 加载账号
    accounts = load_accounts(accounts_file)
    logger.info(f"Loaded {len(accounts)} accounts")
    
    if not accounts:
        logger.error("No accounts found! Please check accounts.json")
        return 1
    
    # 监控列表
    comics_raw = config.get("comics", [])
    comics = []
    
    for comic in comics_raw:
        normalized = normalize_comic_config(comic, logger)
        if normalized:
            comics.append(normalized)
    
    if args.series:
        comics = [c for c in comics if c.get("series_id") == args.series]
    
    if not comics:
        logger.warning("No comics to monitor")
        return 0
    
    logger.info(f"Monitoring {len(comics)} series")
    
    # 执行监控
    total_success = 0
    for comic in comics:
        if not comic.get("enabled", True):
            continue
        
        series_id = comic.get("series_id")
        series_name = comic.get("name", series_id)
        
        success, accounts = monitor_series(
            series_id, series_name, accounts, accounts_file, config, logger
        )
        total_success += success
    
    logger.info("=" * 60)
    logger.info(f"Monitor completed: {total_success} episodes downloaded")
    logger.info("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
