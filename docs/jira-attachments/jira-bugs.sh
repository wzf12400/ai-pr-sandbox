#!/bin/bash
# Jira Bug 查询脚本 — 局域网 Jira Server
# 用法:
#   ./dev/jira-bugs.sh                  # 查询 IIT 项目所有未关闭 Bug
#   ./dev/jira-bugs.sh -a chunxu.wu     # 只看分配给我的
#   ./dev/jira-bugs.sh -k "倒计时"       # 按关键词搜索
#   ./dev/jira-bugs.sh -s all           # 查看所有状态
#   ./dev/jira-bugs.sh -p WX            # 查其他项目
#   ./dev/jira-bugs.sh -i IIT-1110      # 查看单个 Issue 详情

JIRA_URL="http://10.11.11.156"
JIRA_USER="chunxu.wu"
JIRA_PASS="密码放这里"
PROJECT="IIT"
ASSIGNEE=""
STATUS="open"
KEYWORD=""
ISSUE_KEY=""
MAX_RESULTS=50

while getopts "p:a:s:k:i:n:h" opt; do
    case $opt in
        p) PROJECT="$OPTARG" ;;
        a) ASSIGNEE="$OPTARG" ;;
        s) STATUS="$OPTARG" ;;
        k) KEYWORD="$OPTARG" ;;
        i) ISSUE_KEY="$OPTARG" ;;
        n) MAX_RESULTS="$OPTARG" ;;
        h)
            echo "用法: $0 [-p 项目Key] [-a 指派人] [-s 状态] [-k 关键词] [-i IssueKey] [-n 最大条数]"
            echo "  -p  项目 Key (默认: IIT)"
            echo "  -a  指派人用户名 (如: chunxu.wu)"
            echo "  -s  状态: open(默认)|all|具体状态名"
            echo "  -k  标题关键词搜索"
            echo "  -i  查看单个 Issue 详情"
            echo "  -n  最大返回条数 (默认: 50)"
            exit 0
            ;;
        *) exit 1 ;;
    esac
done

AUTH=$(echo -n "${JIRA_USER}:${JIRA_PASS}" | base64)

if [ -n "$ISSUE_KEY" ]; then
    curl -s "${JIRA_URL}/rest/api/2/issue/${ISSUE_KEY}?fields=summary,status,priority,assignee,reporter,created,updated,description,comment,attachment,labels" \
        -H "Authorization: Basic ${AUTH}" \
        -H "Content-Type: application/json" | python3 -c "
import json, sys
d = json.load(sys.stdin)
f = d['fields']
print(f\"# {d['key']}: {f.get('summary','')}\")
print(f\"状态: {f.get('status',{}).get('name','N/A')}\")
print(f\"优先级: {f.get('priority',{}).get('name','N/A')}\")
print(f\"指派人: {(f.get('assignee') or {}).get('displayName','未分配')}\")
print(f\"报告人: {(f.get('reporter') or {}).get('displayName','N/A')}\")
print(f\"创建: {f.get('created','')[:19].replace('T',' ')}\")
print(f\"更新: {f.get('updated','')[:19].replace('T',' ')}\")
labels = f.get('labels',[])
if labels: print(f\"标签: {', '.join(labels)}\")
print(f\"\n## 描述\n{f.get('description','(无)')}\")
comments = f.get('comment',{}).get('comments',[])
if comments:
    print(f\"\n## 评论 ({len(comments)})\")
    for c in comments:
        print(f\"\n### {c.get('author',{}).get('displayName','?')} ({c.get('created','')[:19].replace('T',' ')})\")
        print(c.get('body',''))
attachments = f.get('attachment',[])
if attachments:
    print(f\"\n## 附件 ({len(attachments)})\")
    for a in attachments:
        print(f\"- {a['filename']} ({a.get('mimeType','')}) → {a.get('content','')}\")
"
    exit 0
fi

# Build JQL
JQL="issuetype = Bug"
[ -n "$PROJECT" ] && JQL="$JQL AND project = $PROJECT"
[ -n "$ASSIGNEE" ] && JQL="$JQL AND assignee = $ASSIGNEE"
if [ "$STATUS" = "open" ]; then
    JQL="$JQL AND status not in (Done, Closed, Resolved)"
elif [ "$STATUS" != "all" ]; then
    JQL="$JQL AND status = \"$STATUS\""
fi
[ -n "$KEYWORD" ] && JQL="$JQL AND summary ~ \"$KEYWORD\""
JQL="$JQL ORDER BY created DESC"

ENCODED_JQL=$(python3 -c "import urllib.parse; print(urllib.parse.quote('$JQL'))")

curl -s "${JIRA_URL}/rest/api/2/search?jql=${ENCODED_JQL}&maxResults=${MAX_RESULTS}&fields=summary,status,priority,assignee,created,reporter" \
    -H "Authorization: Basic ${AUTH}" \
    -H "Content-Type: application/json" | python3 -c "
import json, sys
data = json.load(sys.stdin)
total = data.get('total', 0)
issues = data.get('issues', [])
print(f'共 {total} 个 Bug (显示 {len(issues)} 个)\n')
for i in issues:
    f = i['fields']
    assignee = (f.get('assignee') or {}).get('displayName', '未分配')
    reporter = (f.get('reporter') or {}).get('displayName', 'N/A')
    status = f.get('status',{}).get('name','N/A')
    priority = f.get('priority',{}).get('name','N/A')
    created = f.get('created','')[:10]
    print(f'[{i[\"key\"]}] {f.get(\"summary\",\"\")}')
    print(f'  状态: {status} | 优先级: {priority} | 指派: {assignee} | 报告人: {reporter} | 创建: {created}')
    print()
"
