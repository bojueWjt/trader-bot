#!/bin/bash
# 用法: ./upload_report.sh <local_html_path>
# 上传到阿里云 /var/www/report/

REMOTE="root@39.108.225.60"
REMOTE_DIR="/var/www/report"
KEY="${HOME}/.ssh/id_ed25519"

# 检查参数
if [ $# -eq 0 ]; then
    echo "用法: $0 <local_html_path>"
    exit 1
fi

LOCAL_FILE="$1"

# 检查文件是否存在
if [ ! -f "$LOCAL_FILE" ]; then
    echo "错误: 文件不存在: $LOCAL_FILE"
    exit 1
fi

# 上传
scp -i "$KEY" "$LOCAL_FILE" "${REMOTE}:${REMOTE_DIR}/"

if [ $? -eq 0 ]; then
    BASENAME=$(basename "$1")
    echo "Uploaded: https://blog.balen.wang/report/${BASENAME}"
else
    echo "上传失败"
    exit 1
fi
