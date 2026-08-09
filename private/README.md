# 私有授权后台边界

本目录只公开无数据的数据库 schema 和安全说明。实际联系人、邮件、授权回复、证据附件、内部备注、服务凭据和导出文件不得进入 Git。

推荐边界：

```text
公开 GitHub / Netlify
  └── 脱敏 paper 状态、统计、译文和公开贡献者

私有 Supabase/Postgres + 私有对象存储
  ├── contacts
  ├── authorization_requests
  ├── authorization_events
  └── 邮件证据对象、哈希和访问控制
```

`schema.sql` 默认给所有表启用 Row Level Security，但不创建公开访问 policy。因此使用 anon key 或浏览器前端不能读取这些表；服务端也必须使用最小权限凭据，并把“发送邮件”和“确认授权范围”分配给不同角色。

本地开发要求：

- 只复制根目录 `.env.example` 为未跟踪的 `.env`；
- 不使用真实作者数据做公开演示或 CI；
- 测试数据使用 `example.invalid` 域名；
- 导出时默认删除邮箱、邮件正文、私人备注和对象 URL；
- 数据备份、对象存储和日志必须采用访问控制和保留期限。

任何私有授权结果同步到公共 manifest 前，必须经过人工范围复核，并只同步脱敏状态、依据类型、条件摘要和更新时间。
