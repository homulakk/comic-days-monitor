#!/usr/bin/env python3
"""
accounts.json 同步工具

用途：只同步 email/password，保留服务器本地的 pt/ticket_used

使用：
    # 从本地推到服务器
    python sync_accounts.py --source ./accounts.json --target user@server:/path/accounts.json
    
    # 或者先生成纯净版本，再用 scp
    python sync_accounts.py --strip ./accounts.json -o accounts_clean.json
"""

import json
import argparse
from pathlib import Path
from datetime import datetime


def strip_account_data(accounts_file: str, output_file: str = None) -> dict:
    """提取 accounts.json 中只读字段（email/password）
    
    返回只包含 email/password 的纯净账号列表
    """
    with open(accounts_file, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    clean_accounts = []
    for acc in data.get('accounts', []):
        clean_acc = {
            'email': acc.get('email'),
            'password': acc.get('password')
        }
        # 只保留这两个字段
        clean_accounts.append(clean_acc)
    
    result = {'accounts': clean_accounts}
    
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Written {len(clean_accounts)} accounts to {output_file}")
    
    return result


def merge_accounts(source_file: str, target_file: str, output_file: str = None) -> dict:
    """合并账号数据
    
    - 从 source 读取 email/password（本地新账号）
    - 从 target 读取 pt（服务器已消费的 PT）
    - PT 取较小值（因为 PT 只减不增）
    - 新账号直接添加
    """
    with open(source_file, 'r', encoding='utf-8') as f:
        source_data = json.load(f)
    
    with open(target_file, 'r', encoding='utf-8') as f:
        target_data = json.load(f)
    
    # 建立 email -> target account 的映射
    target_by_email = {}
    for acc in target_data.get('accounts', []):
        target_by_email[acc.get('email')] = acc
    
    merged_accounts = []
    new_count = 0
    updated_count = 0
    
    for src_acc in source_data.get('accounts', []):
        email = src_acc.get('email')
        merged = {
            'email': email,
            'password': src_acc.get('password')
        }
        
        if email in target_by_email:
            # 已存在的账号
            tgt_acc = target_by_email[email]
            src_pt = src_acc.get('pt')
            tgt_pt = tgt_acc.get('pt')
            
            # PT 取较小值（真实消费后的值更准确）
            if src_pt is not None and tgt_pt is not None:
                merged['pt'] = min(src_pt, tgt_pt)
            elif tgt_pt is not None:
                merged['pt'] = tgt_pt
            elif src_pt is not None:
                merged['pt'] = src_pt
            
            updated_count += 1
        else:
            # 新账号，保留 PT
            if 'pt' in src_acc:
                merged['pt'] = src_acc['pt']
            new_count += 1
        
        merged_accounts.append(merged)
    
    result = {'accounts': merged_accounts}
    
    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"Merged {len(merged_accounts)} accounts: {updated_count} updated, {new_count} new")
    
    return result


def show_diff(file1: str, file2: str):
    """显示两个 accounts.json 的差异"""
    with open(file1, 'r', encoding='utf-8') as f:
        data1 = json.load(f)
    with open(file2, 'r', encoding='utf-8') as f:
        data2 = json.load(f)
    
    emails1 = {a['email'] for a in data1.get('accounts', [])}
    emails2 = {a['email'] for a in data2.get('accounts', [])}
    
    only_in_1 = emails1 - emails2
    only_in_2 = emails2 - emails1
    common = emails1 & emails2
    
    print(f"文件1 ({file1}): {len(emails1)} 账号")
    print(f"文件2 ({file2}): {len(emails2)} 账号")
    print(f"共同账号: {len(common)}")
    print(f"只在文件1: {len(only_in_1)}")
    print(f"只在文件2: {len(only_in_2)}")
    
    if only_in_1:
        print(f"\n只在文件1的账号: {list(only_in_1)[:5]}...")
    if only_in_2:
        print(f"只在文件2的账号: {list(only_in_2)[:5]}...")


def main():
    parser = argparse.ArgumentParser(description="accounts.json 同步工具")
    
    # 模式1: 提取纯净版本
    parser.add_argument('--strip', metavar='FILE', help='提取 email/password 到新文件')
    parser.add_argument('-o', '--output', help='输出文件路径')
    
    # 模式2: 合并
    parser.add_argument('--merge-source', help='源文件（email/password）')
    parser.add_argument('--merge-target', help='目标文件（保留 pt/ticket_used）')
    
    # 模式3: 对比
    parser.add_argument('--diff', nargs=2, metavar=('FILE1', 'FILE2'), help='对比两个文件')
    
    args = parser.parse_args()
    
    if args.strip:
        output = args.output or 'accounts_clean.json'
        strip_account_data(args.strip, output)
    
    elif args.merge_source and args.merge_target:
        output = args.output or args.merge_target
        merge_accounts(args.merge_source, args.merge_target, output)
    
    elif args.diff:
        show_diff(args.diff[0], args.diff[1])
    
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
