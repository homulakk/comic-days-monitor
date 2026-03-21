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
        "last_episode_id": None,
        "last_checked": None,
        "purchased": {},
        "pt_used": {}  # {email: total_pt_used}
    }


def save_state(state: dict, state_dir: Path):
    """保存作品状态"""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / f"{state['series_id']}.json"
    state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')


def load_accounts(accounts_file: Path) -> List[dict]:
    """加载账号池"""
    if not accounts_file.exists():
        return []
    
    try:
        data = json.loads(accounts_file.read_text(encoding='utf-8'))
        return data.get('accounts', [])
    except:
        return []


# ============================================================================
# 监控逻辑
# ============================================================================

def check_new_episodes(series_id: str, state: dict, logger) -> List[dict]:
    """检查新章节"""
    episodes = fetch_episodes_from_atom(series_id)
    
    if not episodes:
        logger.warning(f"No episodes found for {series_id}")
        return []
    
    # 找到上次检查后的新章节
    last_ep_id = state.get("last_episode_id")
    
    if last_ep_id:
        # 找到上次章节的位置
        last_index = -1
        for i, ep in enumerate(episodes):
            if ep['id'] == last_ep_id:
                last_index = i
                break
        
        # 返回之后的所有章节（最新的在前面）
        if last_index > 0:
            return episodes[:last_index]
        elif last_index == 0:
            return []  # 没有新章节
    
    # 没有记录，返回全部
    return episodes


def find_available_account(accounts: List[dict], series_id: str, state: dict, logger) -> Optional[dict]:
    """找到可用的账号"""
    for acc in accounts:
        email = acc.get('email')
        
        # 检查票据是否可用 (23小时充能)
        # 从 state 中获取票据使用时间
        ticket_info = state.get("ticket_used", {}).get(email)
        if ticket_info:
            try:
                last_used = datetime.fromisoformat(ticket_info)
                if datetime.now() - last_used < timedelta(hours=23):
                    continue  # 充能中
            except:
                pass
        
        return acc
    
    return None


def process_episode(
    episode_id: str,
    series_id: str,
    series_name: str,
    accounts: List[dict],
    state: dict,
    download_dir: Path,
    state_dir: Path,
    logger
) -> bool:
    """处理单个章节"""
    session = requests.Session()
    
    # 先检查是否已购买/免费
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
        return True
    
    # 需要购买
    account = find_available_account(accounts, series_id, state, logger)
    
    if not account:
        logger.warning(f"  No available account for {episode_id}")
        return False
    
    email = account['email']
    password = account['password']
    
    # 登录
    if not http_login(session, email, password):
        logger.error(f"  Login failed: {email}")
        return False
    
    # 获取实际 PT
    user_info = http_get_user_info(session)
    actual_pt = user_info.get("point", {}).get("total", 0)
    
    # 计算已使用的 PT
    pt_used = state.get("pt_used", {}).get(email, 0)
    available_pt = actual_pt - pt_used
    
    logger.info(f"  Account: {email[:20]}... PT: {available_pt} (actual: {actual_pt}, used: {pt_used})")
    
    # 尝试票据购买
    if ep_info['has_ticket']:
        success, msg = http_use_ticket(session, episode_id)
        if success:
            # 记录票据使用
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
            return True
        elif "TICKET_NOT_CHARGED" in msg:
            logger.info(f"  Ticket recharging, trying PT...")
        else:
            logger.warning(f"  Ticket purchase failed: {msg}")
    
    # 尝试 PT 购买
    if ep_info['price'] and available_pt >= ep_info['price']:
        success, msg = http_purchase_pt(session, episode_id)
        if success:
            # 记录 PT 使用
            state.setdefault("pt_used", {})[email] = pt_used + ep_info['price']
            result = download_episode(session, episode_id, series_name, download_dir)
            state["purchased"][episode_id] = {
                "title": result.get("title"),
                "mode": "pt",
                "account": email,
                "price": ep_info['price'],
                "downloaded": result["success"],
                "total": result["total"],
                "at": datetime.now().isoformat()
            }
            save_state(state, state_dir)
            return True
        else:
            logger.warning(f"  PT purchase failed: {msg}")
    else:
        logger.warning(f"  Insufficient PT: {available_pt} < {ep_info['price']}")
    
    return False


def monitor_series(
    series_id: str,
    series_name: str,
    accounts: List[dict],
    config: dict,
    logger
) -> int:
    """监控单部漫画"""
    base_dir = get_base_dir()
    download_dir = base_dir / config.get("monitor", {}).get("download_dir", "./downloads")
    state_dir = base_dir / config.get("monitor", {}).get("state_dir", "./states")
    
    # 加载状态
    state = load_state(series_id, state_dir)
    state["series_name"] = series_name
    
    logger.info(f"Checking: {series_name} ({series_id})")
    
    # 检查新章节
    new_episodes = check_new_episodes(series_id, state, logger)
    
    if not new_episodes:
        logger.info(f"  No new episodes")
        state["last_checked"] = datetime.now().isoformat()
        save_state(state, state_dir)
        return 0
    
    logger.info(f"  Found {len(new_episodes)} new episodes")
    
    # 按时间排序（旧的先处理）
    new_episodes.reverse()
    
    # 只处理最新的 N 话
    latest_only = config.get("monitor", {}).get("latest_only", 1)
    if latest_only > 0:
        new_episodes = new_episodes[-latest_only:]
        logger.info(f"  Processing latest {len(new_episodes)} episodes")
    
    # 处理每个新章节
    success_count = 0
    for ep in new_episodes:
        logger.info(f"  Episode: {ep['id']} - {ep['title'][:30] if ep['title'] else 'N/A'}")
        
        success = process_episode(
            ep['id'],
            series_id,
            series_name,
            accounts,
            state,
            download_dir,
            state_dir,
            logger
        )
        
        if success:
            success_count += 1
            state["last_episode_id"] = ep['id']
    
    state["last_checked"] = datetime.now().isoformat()
    save_state(state, state_dir)
    
    return success_count


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
    comics = config.get("comics", [])
    
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
        
        success = monitor_series(series_id, series_name, accounts, config, logger)
        total_success += success
    
    logger.info("=" * 60)
    logger.info(f"Monitor completed: {total_success} episodes downloaded")
    logger.info("=" * 60)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
