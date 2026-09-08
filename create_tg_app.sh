#!/usr/bin/env bash
# my.telegram.org 创建应用一键重试脚本
# 用法: ./create_tg_app.sh <stel_token> [标题] [短名前缀]
# stel_token 获取: 浏览器登录 my.telegram.org → F12 → Application → Cookies → stel_token
TOKEN="$1"
TITLE="${2:-wokea$(date +%m%d)}"
PREFIX="${3:-wokea$(date +%H%M)}"
if [ -z "$TOKEN" ]; then echo "用法: $0 <stel_token> [标题] [短名前缀]"; exit 1; fi

PAGE=$(curl -s --max-time 15 "https://my.telegram.org/apps/" -H "Cookie: stel_token=$TOKEN")
HASH=$(grep -o 'name="hash" value="[^"]*"' <<<"$PAGE" | sed 's/.*value="//;s/"//')
if [ -z "$HASH" ]; then
  echo "token 无效或已过期(页面里没有 hash 表单)。若页面已显示 api_id/api_hash,说明早就创建成功了。"
  echo "$PAGE" | grep -o 'App api_id.*' | head -3
  exit 2
fi
echo "hash=$HASH"

SHORT="${PREFIX}$(shuf -er -n4 A-Za-z0-9 | tr -d '/+')"
SHORT=$(echo "$SHORT" | tr -dc 'A-Za-z0-9')
echo "尝试: title=$TITLE shortname=$SHORT"

RES=$(curl -s --max-time 15 -X POST "https://my.telegram.org/apps/create" \
  -H "Cookie: stel_token=$TOKEN" -H "Referer: https://my.telegram.org/apps/" \
  -H "X-Requested-With: XMLHttpRequest" \
  --data-urlencode "hash=$HASH" --data-urlencode "app_title=$TITLE" \
  --data-urlencode "app_shortname=$SHORT" --data-urlencode "app_url=" \
  --data-urlencode "app_platform=desktop" --data-urlencode "app_desc=personal tool")

if [ -z "$RES" ] || [ "$RES" = "ERROR" ]; then
  echo "结果: $RES —— 被服务端拒绝(新账号常见)。过几天再试,或换注册较久的账号。"
else
  echo "结果: $RES"
fi
echo "--- 验证是否成功 ---"
curl -s --max-time 15 "https://my.telegram.org/apps/" -H "Cookie: stel_token=$TOKEN" | grep -o 'App api_id[^<]*\|App api_hash[^<]*\|app_create_form' | sort -u | head -5
