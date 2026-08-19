// Extension: jira
// Query Jira bugs from LAN-deployed Jira Server

import { joinSession } from "@github/copilot-sdk/extension";

const JIRA_BASE_URL = "http://10.11.11.156";
const JIRA_USERNAME = "chunxu.wu";
const JIRA_PASSWORD = "密码放这里";
const AUTH_HEADER = "Basic " + Buffer.from(`${JIRA_USERNAME}:${JIRA_PASSWORD}`).toString("base64");

async function jiraFetch(path) {
    const res = await fetch(`${JIRA_BASE_URL}${path}`, {
        headers: {
            Authorization: AUTH_HEADER,
            "Content-Type": "application/json",
        },
    });
    if (!res.ok) {
        throw new Error(`Jira API error: HTTP ${res.status} - ${await res.text()}`);
    }
    return res.json();
}

function formatIssue(issue) {
    const f = issue.fields;
    const assignee = f.assignee?.displayName || "未分配";
    const reporter = f.reporter?.displayName || "N/A";
    const created = (f.created || "").slice(0, 10);
    const priority = f.priority?.name || "N/A";
    const status = f.status?.name || "N/A";
    return `[${issue.key}] ${f.summary}\n  状态: ${status} | 优先级: ${priority} | 指派: ${assignee} | 报告人: ${reporter} | 创建: ${created}`;
}

const session = await joinSession({
    tools: [
        {
            name: "jira_query_bugs",
            description: "查询 Jira 项目中的 Bug 列表。可按项目、状态、指派人等条件筛选。返回 Bug 的标题、状态、优先级、指派人等信息。",
            skipPermission: true,
            parameters: {
                type: "object",
                properties: {
                    project: {
                        type: "string",
                        description: "Jira 项目 Key（如 IIT、WX）。不填则查询所有项目",
                    },
                    assignee: {
                        type: "string",
                        description: "指派人用户名（如 chunxu.wu）。不填则不限指派人。使用 'currentUser()' 查询当前登录用户",
                    },
                    status: {
                        type: "string",
                        description: "Bug 状态筛选。可选值: open（未关闭）、all（全部）、或具体状态名如 '开放'、'重新打开'、'正在处理'",
                        default: "open",
                    },
                    max_results: {
                        type: "number",
                        description: "最大返回条数，默认 50",
                        default: 50,
                    },
                    keyword: {
                        type: "string",
                        description: "按关键词搜索 Bug 标题（summary 字段包含）",
                    },
                },
            },
            handler: async (args) => {
                const conditions = ["issuetype = Bug"];

                if (args.project) {
                    conditions.push(`project = ${args.project}`);
                }
                if (args.assignee) {
                    conditions.push(`assignee = ${args.assignee}`);
                }
                if (!args.status || args.status === "open") {
                    conditions.push("status not in (Done, Closed, Resolved)");
                } else if (args.status !== "all") {
                    conditions.push(`status = "${args.status}"`);
                }
                if (args.keyword) {
                    conditions.push(`summary ~ "${args.keyword}"`);
                }

                const jql = conditions.join(" AND ") + " ORDER BY created DESC";
                const maxResults = args.max_results || 50;
                const encodedJql = encodeURIComponent(jql);
                const fields = "summary,status,priority,assignee,created,reporter,description";

                const data = await jiraFetch(
                    `/rest/api/2/search?jql=${encodedJql}&maxResults=${maxResults}&fields=${fields}`
                );

                const total = data.total || 0;
                const issues = data.issues || [];

                if (issues.length === 0) {
                    return `未找到匹配的 Bug（JQL: ${jql}）`;
                }

                const lines = [`查询到 ${total} 个 Bug（显示前 ${issues.length} 个）\nJQL: ${jql}\n`];
                for (const issue of issues) {
                    lines.push(formatIssue(issue));
                }
                return lines.join("\n");
            },
        },
        {
            name: "jira_get_issue",
            description: "获取单个 Jira Issue 的详细信息，包括描述、评论、附件等。传入 Issue Key（如 IIT-1110）。",
            skipPermission: true,
            parameters: {
                type: "object",
                properties: {
                    issue_key: {
                        type: "string",
                        description: "Jira Issue Key，如 IIT-1110",
                    },
                },
                required: ["issue_key"],
            },
            handler: async (args) => {
                const data = await jiraFetch(
                    `/rest/api/2/issue/${args.issue_key}?fields=summary,status,priority,assignee,reporter,created,updated,description,comment,attachment,labels,components,fixVersions`
                );

                const f = data.fields;
                const lines = [
                    `# ${data.key}: ${f.summary}`,
                    "",
                    `状态: ${f.status?.name || "N/A"}`,
                    `优先级: ${f.priority?.name || "N/A"}`,
                    `指派人: ${f.assignee?.displayName || "未分配"}`,
                    `报告人: ${f.reporter?.displayName || "N/A"}`,
                    `创建时间: ${(f.created || "").slice(0, 19).replace("T", " ")}`,
                    `更新时间: ${(f.updated || "").slice(0, 19).replace("T", " ")}`,
                ];

                if (f.labels?.length) {
                    lines.push(`标签: ${f.labels.join(", ")}`);
                }
                if (f.components?.length) {
                    lines.push(`组件: ${f.components.map((c) => c.name).join(", ")}`);
                }
                if (f.fixVersions?.length) {
                    lines.push(`修复版本: ${f.fixVersions.map((v) => v.name).join(", ")}`);
                }

                lines.push("", "## 描述", f.description || "(无描述)");

                const comments = f.comment?.comments || [];
                if (comments.length > 0) {
                    lines.push("", `## 评论 (${comments.length})`);
                    for (const c of comments) {
                        const author = c.author?.displayName || "Unknown";
                        const time = (c.created || "").slice(0, 19).replace("T", " ");
                        lines.push(`\n### ${author} (${time})`, c.body || "");
                    }
                }

                const attachments = f.attachment || [];
                if (attachments.length > 0) {
                    lines.push("", `## 附件 (${attachments.length})`);
                    for (const a of attachments) {
                        lines.push(`- ${a.filename} (${a.mimeType}, ${(a.size / 1024).toFixed(1)}KB) → ${a.content}`);
                    }
                }

                return lines.join("\n");
            },
        },
        {
            name: "jira_list_projects",
            description: "列出 Jira 上所有可访问的项目，返回项目 Key 和名称。",
            skipPermission: true,
            parameters: { type: "object", properties: {} },
            handler: async () => {
                const data = await jiraFetch("/rest/api/2/project");
                if (!data.length) return "未找到可访问的项目";
                const lines = [`共 ${data.length} 个项目:\n`];
                for (const p of data) {
                    lines.push(`[${p.key}] ${p.name}`);
                }
                return lines.join("\n");
            },
        },
    ],
});

await session.log("Jira extension loaded (LAN: 10.11.11.156)");
