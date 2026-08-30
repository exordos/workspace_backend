# Messenger未读的投影升级和倒退

本运行簿应用了迁移引入的查询计划更正
`0149-split-messenger-unread-read-state-branches-c84ae9.py`. 只有迁移
取代视图定义;它不会重写消息或读状态数据.

生产执行需要单独的更改批准.
程序首先在一个孤立的环境恢复从一个代表
数据库备份.

## 访问验证审计

边界流访问辅助器只读取流,其用户绑定,以及
规范外部访问投影. 它用于验证,
不要使用未读过的计数器:

- 消息目标验证;
- 创建主题和主题通知的验证;
- 流程初始更新和流程通知验证;
- 创建文件项目;
- 提供者主题-通知验证.

消息列表已经通过可规的用户消息投影范围
并不调用流快照助手. 文件访问使用文件ACL和
反应路径故意保持全可见的信息
快照,因为他们的响应,事件和提供者有效负载消耗消息
完整的流快照也保持在阅读操作,绑定事件的粉丝,
流删除,和突变后的反应/event路径,其中计数或
需要公开字段.

## 预先条件

1. 确认部署后端包含迁移 `0149`,并且 `0148`
   唯一申请的父母.
2. 保存 `pg_get_viewdef()`输出这些视图到操作员控制
   安全位置:
   - `m_workspace_user_unread_messages_base_v1`
   - `m_workspace_user_topic_unread_counts_v1`
   - `m_unread_user_messages`
   - `m_workspace_user_streams`
   - `m_workspace_user_topics_view`
   - `m_folders_view`
3. 记录仅总读状态模式数量:

   ```sql
   SELECT COALESCE(mode, 'legacy') AS mode, COUNT(*)
   FROM m_workspace_read_state_projects_v1
   GROUP BY COALESCE(mode, 'legacy')
   ORDER BY mode;
   ```

4. 检查 PostgreSQL 连接预算. 两个 Messenger API 工人
   每个工艺池最多需要两个,最多需要四个 Messenger API
   计算所有其他服务池,活动维护会话,
   并且在启用第二个工人之前保留连接.
5. 捕获当前的 499/504 速度, PostgreSQL 等待事件,临时文件
   计数器,以及p50/p95用于精确的流查询,流收集,以及
   提供者批量应用和承诺的时间.
6. 确认滚回命令和保存的视图定义可用
   在休息服务之前.

## 升级

1. 静止 Messenger API 流量和提供商交付. 保持 PostgreSQL
   并且防止另一个迁移运行器启动.
2. 仅使用具有限度 DDL 等待的新迁移:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-apply-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

   时间止息是失败的,不是一个理由去除限制或等待
   调查封锁交易,并从第一个位置重新开始.
   预先检查.
3. 确认迁移行是应用的,六个依赖视图可以
   选择 `LIMIT 0`.
4. 启动 Messenger API 和提供商交付. 确认 PostgreSQL 会议
   对于 Messenger API 和提供商使用不同的 `application_name` 值
   控制
5. 执行服务器设置,流列表,
   查看流的确切,并列出消息列表. 不要创建消息或转变阅读
   在接受通行证期间.
6. 运行 `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` 清洁代表
   精确流量和流量收集查询:
   - 遗留的分支机构使用
     `m_workspace_unread_flags_user_message_idx`;
   - 没有访问 `m_workspace_messages`;
   - 没有临时块.
7. 比较升级后的 499/504 速度,等待事件,临时文件三角,
   确切查询 p50/p95,流收集 p50/p95,提供者批量适用
   并且与基线进行持续时间.

## 倒车

倒退只会改变迁移 `0149`;不要倒退后端发布或
作为一个事件解决方案,允许读状态压缩.

1. 静止 Messenger API 流量和提供者交付再次.
2. 仅限于迁移`0149`的绑定 DDL 等待和降级:

   ```bash
   PGOPTIONS='-c lock_timeout=5s -c statement_timeout=60s' \
     .tox/develop/bin/ra-rollback-migration \
       --config-file <runtime-config> \
       --path migrations \
       --migration 0149-split-messenger-unread-read-state-branches-c84ae9.py
   ```

3. 与保存的数据库和主题数值定义进行比较
   升级前的定义. 下级恢复了0.1.44混合模式
   定义并不会改变读取状态数据.
4. 启动服务并重复相同的仅读健康检查和总
   远距离测量比较.

## 地方性能的检查

仅对一个可用数据库运行交易基准,其名称
包含 `test`. 它取代视图并插入交易内部的固定行
在出口前被卷回:

```bash
WORKSPACE_TEST_DB_URL='<disposable-database-url>' \
  .tox/develop/bin/python \
    workspace/tests/scale/benchmark_unread_projection.py \
    --messages 250000 --unread 100
```

没有JSON输出省略查询的预言和固定标识符.
清理出口在 CASSI 测试运行档案中,而不是在这个服务存储库中.
