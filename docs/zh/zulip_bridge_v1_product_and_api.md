# Zulip桥梁v1:产品要求和API边界

状态: **已批准的产品要求和 API边界;
通过所需的接收计划**进行并进行监控.

本文档定义了Workspace的第一个外部消息传递器集成.
它是故意与当前的 Messenger API合同分开的
[`workspace_api.md`](workspace_api.md)功能分支包含
相关的 API, PostgreSQL存储,提供商 HTTP,桥梁和UI
执行,但
功能还没有准备好启用,直到每个需要的门进入
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md) 通过.

公共账户API和共享桥梁合同是提供商中立的.
提供商特定的帐户有效负载由动态类型 RestAlchemy 表示
模型. Zulip 是第一个提供商,
`workspace-zulip-bridge`元素是第一个实现.
邮件,日历,和其他帐户类型扩展选项器,以新的类型
它们的模式,而不是在共同的服务提供者中添加特定的字段或路线.
资源.

## 1. 目标

- 让一个 Workspace 用户在一个领域中连接一个个人 Zulip 帐户.
- 项目选择 Zulip对话到普通 Workspace 流和
  没有创建一个单独的外部收件箱产品表面.
- 提供双向同步V1能力集.
- 保存正规Workspace架构:PostgreSQL存储正规
  消息和Messenger事件数据;文件使用对象存储和消息
  只有URN.
- 保持提供者桥独立部署并防止它阅读
  不相关的 Workspace 数据库行或 S3 对象.
- 保持提供商的凭证在静止状态下加密,
  管理员,日志,以及普通的 Workspace API 响应.
- 让原生 Workspace 消息在桥梁下降时继续工作.

## 2. V1 没有目标

- 调查,打字指标,电话和出席同步.
- 桥梁元件的可用性很高.
- 其他提供者,不包括 Zulip.
- 产品级审计日志. 运营日志和总体健康保持在
  但它们不得包含凭证或消息内容.
- 备份执行.备份由另一个子系统提供.
- 与删除的提供者 API 或隐藏的遗留UI路径的兼容性
  `/providers/`现在我`/external_users/`.

## 3. 产品的经过批准的行为

### 3.1 账户所有权和生命周期

- 外部帐户属于一个 Workspace 用户,并且是全球性的
  让我们来看看这个领域的用户.
- 用户最多可以有一个提供商类型的外部帐户.
  意思是最多一个 Zulip 账户.
- Zulip设置需要一个 HTTPS服务器 URL,电子邮件地址和 API 键.
- 凭证是仅可写入的. API可能报告存在凭证,但
  永远不会返回 API 密钥或加密的凭证信封.
- `Disconnect` 停止同步,同时保留仅读投影.
- `Delete` 删除凭证,映射,预测实体,排队工作,
  提供商所拥有的复制文件.
- IAM 禁用暂停同步并隐藏帐户. IAM 删除.
  清除它使用与 `Delete`相同的破坏性语义.

### 3.2 聊天选择和项目分配

- 业主可以选择单个外部聊天或选择 `all`.
- `all`是动态的:后来创建的聊天会自动选择.
- 业主选择一个帐户的历史深度: `new`, `7_days`,
  `30_days`, `90_days`没有`all`默认是`30_days`.
- 每个选定的外部聊天都属于一个 Workspace 项目.
- 帐户对新选出的聊天有默认项目.
- 将现有投影转移到另一个项目是从产物到原子的
  视角和保存Workspace UUID,历史,读取状态和提供商
  实现必须产生源项目删除和
  目标项目创建快照;它不能暴露一个中间状态
  预测不属于任何项目或两个项目.

### 3.3 Zulip到Workspace映射

| Zulip 实体  | Workspace 投影                                                        |
| ------------- | --------------------------------------------------------------------------- |
| 道       | 流量                                                                      |
| 主题         | 预计流中的主题                                               |
| 一对一的DM | 个人流程与两个参与者和一个默认主题 |
| 组DM      | 具有一个默认主题的私人组流                                 |
| Zulip 用户    | 根据提供者领域和提供者用户ID的范围确定稳定的身份                |

标准身份密钥是 `(提供者, provider_realm_uuid,
provider_user_id) `. UUID域和认证的帐户用户ID来自
事件队列注册 Zulip,因此改变了服务器 URL,电子邮件地址,
显示名称,或其他 Workspace 帐户不能默默地更改所有权.
电子邮件和显示名字的匹配永远不会被接受为证据.

当一个 IAM 用户成功连接一个 Zulip 帐户时,只有认证的
账户的正规身份 Zulip 与账户所有者 IAM UUID 联系在一起.
其他 Zulip 用户仍然是仅读外部身份,但重复使用一个正规的
Workspace UUID在同一账户中连接到每个帐户.Zulip领域.
相互冲突的验证所有者链接被拒绝,而不会改变任何身份
需要明确的行政解决方案.
身份将合并到其 Messenger 的正规身份中
引用,分配,缓存事件有效负载更新之前重复
链接一个身份到IAM不会允许用户访问
另一个所有者的账户范围预测;
链接 IAM 帐户为该用户贡献流和未读状态.
继续使用 `urn:user:<identity-uuid>`.

### 3.4 同步语义

- 支持双向创建,编辑,删除,反应,阅读状态,提及,
  答案,引号,标记,链接,图像,文件和流/topic 改名
  提供商的能力允许.
- 往外运营使用业主个人账户 Zulip.
- 提供者功能是权威的.不支持的操作是隐藏的;
  暂时无法使用的操作被禁用,并提供安全的解释.
- 最后一次确认操作获胜. 删除同时编辑时的获胜.
- 每个操作都有一个稳定的 UUID 和提供者身份元数据.
- 后备充值从最新到最旧运行. 现场同步首先开始,并且有
  严格的安排优先于出箱重试和后填.
- 首次追赶不会创建桌面通知.
  只有在账户进入现役状态后才启用.
- 每个接受的供应商实体存储入口 `delivery_class` 和
  在其公共提供者元数据中结`notification_eligible`决定.
  `backfill`总是不符合条件.`live`只有当
  帐户通知网关已经打开; 现场消息
  接受的帐户历史仍在追赶,仍然没有通知.
  后续的帐户状态更改从未可以追溯地促进存储的消息.
- 耐用出口箱可以保留可重新测试的操作长达24小时.
  显示 `pending`,然后要么是 `delivered` 或 `failed`. 一个失败的操作可以
  必须重新尝试或丢弃.
- 取消选择或提供者访问丢失取消待处理的工作,并立即
  删除投影和复制的提供者文件.
- 目标健康系统延迟为p95最多5秒.
  `degraded` 在没有同步进展的30秒后.

### 3.5 负损感知内容转换

- 支持的 Zulip 内容转换为正规的 Workspace 标记.
- 没有支持的入口元素使用安全,可读的后备和
  `Open original`链接当 Zulip可以提供一个.
- 原始供应商ID和结构化转换元数据存储在内部
  投影元数据,不被显示为可写的浏览器字段.
- 在已知丢失信息的出发操作之前,UI获得一个
  服务器端的预飞结果需要明确确认.
- 附件被复制到接收系统中. Workspace 收到的存储
  字节加上一个 JSON 侧车在 S3 兼容的存储;消息仅包含
  `urn:*` 参考资料

### 3. 6 治疗方法

- 帐户所有者看到帐户,聊天选择,进展,能力,
  它们的安全错误状态.
- 管理者管理提供商政策,自定义CA证书,
  限制,并紧急暂停/resume行动.
- 域管理员只能看到整体桥/account的状态.
  表面不得暴露凭证,消息内容或用户聊天
  它们的目录.
- 输出 Zulip TLS 使用系统信任存储以及管理员管理
  客户端证书. 主机名验证始终启用;不安全
  禁止使用或跳过验证模式.

## 4. 现有合同约束

执行必须延长现行 Messenger 合同,而不是
恢复历史一体化规范.

- 浏览器API仍然在`/api/workspace/v1/messenger/**`下,使用IAM
  持有人代币.该代币目前提供了一个用户 UUID 和项目ID.
- 提供商管理路线仍然是目前浏览器合同的附加.
- PostgreSQL是权威的消息,共享 Messenger状态,帐户
  设置,加密凭证,提供商队列,以及除重记录.
- 文件复制使用一个单独的,狭窄的二进制传输平面;它不能授予
  桥梁全球 S3 凭证.
- 公共 `provider` 和 `delivery` 字段是保留的,但没有填写
  连续化器的平价性是一个先决条件
  为了启用该功能.
- 浏览器事件标志器是项目和用户范围的可配置
  它们不是桥队列标志器,必须
  不能再用作供应商的出口箱或后填位置.
- 目前的 `ExternalAccount` 剩余是项目范围,允许纯文本
  JSON必须用一个范围模型取代它,
  迁移目标或兼容性合同.

## 5. 公众 Workspace API 提案

根据目前的IAM认证Messenger,建议以下路线:
它们必须生成到 Workspace OpenAPI 和 `@workspace/api`
在UI实现之前,所有UUID收集路线使用标准
Messenger页面化合同.路线由每个外部帐户共享
提供商特定航线是禁止的.

### 5.1 外交账户

| 方法   | 航线                                                         | IAM 许可                            | 目的                                                                      |
| -------- | ------------------------------------------------------------- | ----------------------------------------- | ---------------------------------------------------------------------------- |
| `GET`    | `/external_accounts/`                                         | `workspace.external_account.read`         | 列出当前用户的区域-全球外部帐户.                      |
| `POST`   | `/external_accounts/`                                         | `workspace.external_account.create`       | 创建和验证任何支持的帐户类型,只使用可写凭证. |
| `GET`    | `/external_accounts/{account_uuid}`                           | `workspace.external_account.read`         | 让我们拿出所有者的清洁账户快照.                                  |
| `PUT`    | `/external_accounts/{account_uuid}`                           | `workspace.external_account.update`       | 取代可变的非秘密设置.                                         |
| `POST`   | `/external_accounts/{account_uuid}/actions/reconnect/invoke`  | `workspace.external_account.reconnect`    | 验证并更换仅写凭证,然后恢复.                 |
| `POST`   | `/external_accounts/{account_uuid}/actions/disconnect/invoke` | `workspace.external_account.disconnect`   | 停止同步并保留仅读投影.                                 |
| `DELETE` | `/external_accounts/{account_uuid}`                           | `workspace.external_account.delete`       | 破坏性清除账户并返回 `204`.                            |

已批准的资源形状是一个具有动态`settings`的普通封装
没有任何的.`settings.kind`差异化者选择一个具体的
`AbstractKindModel` 通过
`KindModelSelectorType`. 共同的生命周期,所有者,状态,修订,能力,
时间字段保持在外面.`settings`每种人都拥有自己的联系,
查找和同步设置. API强制执行一个
拥有者最多有一个每个 `settings.kind` 的帐户.

公开 `capabilities` 字段是后端计算的有效账户级别
提供商,桥接实例,领域策略和账户表格的投影
没有原始的心跳描述器,也不暴露
接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接口,接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接接
账户级的行为和状态.

公有功能使用相同稳定的名字空间能力名称的地图
具有 `available`, `revision`, `limits` 的有效描述符,并
一个可选的结构化安全柜 `unavailable_reason`.
暂时被禁用通过账户状态或政策仍然存在
`available=false`;一个缺失的名称意味着资源不支持
客户端不能从原始状态或
提供者类型,当一个有效的描述符存在.

创建,清洁-响应,和重新连接设置选择器是不同的
API类型.一个创建/reconnect类型可能包含仅可写的凭证字段;
它们不能被相应的响应类型串行.
时间表,或其他供应商的意思是注册新的设置类型的模型在
选择器不改变收集路线或添加无效字段
共同资源.

Zulip `POST /external_accounts/`要求:

```json
{
  "uuid": "client-generated-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "api_key": "write-only",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  }
}
```

清理账户响应:

```json
{
  "uuid": "account-uuid",
  "settings": {
    "kind": "zulip",
    "server_url": "https://zulip.example.invalid",
    "email": "owner@example.invalid",
    "selection_mode": "explicit",
    "history_depth": "30_days",
    "default_project_id": "project-uuid"
  },
  "credential_present": true,
  "status": "live",
  "live_ready": true,
  "safe_error": null,
  "capabilities": {},
  "desired_generation": 7,
  "applied_generation": 7,
  "last_progress_at": "2026-07-17T12:00:00Z",
  "created_at": "2026-07-17T11:00:00Z",
  "updated_at": "2026-07-17T12:00:00Z"
}
```

账户状态值是 `connecting`, `backfill`, `live`, `degraded`,
`auth_required`, `disconnected`没有`suspended`.

对于`PUT`使用强`ETag`和需要`If-Match`的修改安全.
Zulip只有这种情况才会改变.`selection_mode`, `history_depth`,以及
`default_project_id` 在 `settings` 中. 服务器 URL,电子邮件,和 API 键更改
只有通过`reconnect`. `settings.kind`和所有者是不可变的.
在动态更新模型中定义自己的可变子集.

### 5.2 外部聊天目录和分配

已批准的目录形状是普通的顶级.Messenger资源.这是
只有广告`chat_catalog`功能的帐户类型;
没有需要邮件或日历等帐户类型来实现它.

| 方法 | 航线                                                 | 目的                                                                |
| ------ | ----------------------------------------------------- | ---------------------------------------------------------------------- |
| `GET`  | `/external_chats/?external_account_uuid=...`          | 列出所有者清洁的提供者聊天目录和分配状态. |
| `GET`  | `/external_chats/{chat_uuid}`                         | 让一个清洁的聊天快照.                                       |
| `POST` | `/external_chats/{chat_uuid}/actions/select/invoke`   | 选择一个聊天,并分配一个项目.                                    |
| `POST` | `/external_chats/{chat_uuid}/actions/deselect/invoke` | 取消工作,并移除投影.                                 |
| `POST` | `/external_chats/{chat_uuid}/actions/move/invoke`     | 原子移动现有投影到另一个项目.             |

资源使用具有动态属性`source`的共同封装.
`source.kind` 选择提供者特定的目录元数据
`KindModelSelectorType`;对于v1,其唯一实现是`zulip`.
字段包括Workspace生成的聊天 UUID,外部帐户 UUID,选择
状态,项目分配,投影 UUID,能力,状态,修改
提供商ID是内部的,永远无法写入,不需要
暴露在外面.

每个聊天的 `capabilities` 字段是后端计算的有效投影
对于那个聊天和帐户. 它可能比账户级预测更窄
因为提供者聊天类型,分配状态或策略可以禁用操作.
用户界面从来没有从原始桥架实例能力地图中导出聊天行为.
后端保留了原始目录描述符,因此暂时
帐户/instance 不可用性可能会禁用有效描述符
破坏恢复后恢复的目录功能.

`select`和`move`接受一个`project_id`; `move`也需要`If-Match`
现在的任务修订. `deselect` 立即取消待处理的工作
每个动作返回一个完全清洁的聊天
现在我们要做什么?

`selection_mode=all`是 Zulip 账户状态,不是一次性批量操作.
后端继续将新发现的聊天分配给`default_project_id`
只有当所有者改变模式时.

### 5.3 外部活动

经批准的耐用操作表面是普通顶层Messenger
资源.它代表了每个外部帐户类型的供应商相关工作,
包括尚未建立或已经建立目标的行动
已删除

| 方法   | 航线                                                        | 目的                                                             |
| -------- | ------------------------------------------------------------ | ------------------------------------------------------------------- |
| `GET`    | `/external_operations/`                                      | 列出所有者正在进行或失败的外部操作.             |
| `GET`    | `/external_operations/{operation_uuid}`                      | 让我们清除运营状态.                                     |
| `POST`   | `/external_operations/{operation_uuid}/actions/retry/invoke` | 试试一个符合条件的失败操作.                                 |
| `DELETE` | `/external_operations/{operation_uuid}`                      | 取消受理待办/failed工作和退还`204`.              |
| `POST`   | `/external_operations/actions/preflight/invoke`              | 退出突变之前的回归能力和损失信息. |

一个操作响应使用一个包含其UUID,外部的共同封装
帐户 UUID,操作,目标类型/UUID,状态,安全错误,重新尝试/discard标志,
尝试和尝试历史,重复风险和重复确认标记,
当安全时原始提供者 URL,调整状态/reason/evidence,修改
时间.`details.kind`模型含有清洁
提供商特定的交付元数据.它不包含原始的提供商有效载荷,
凭证,超出普通授权目标资源的消息内容,
或原始供应商历史匹配.

`delivery`预计资源的总量将持续扩大,以
`external_operation_uuid`, `status`, `safe_error`, `can_retry`, `can_discard`,
`updated_at`, `duplicate_risk`, `retry_requires_confirmation`, `original_url`,
现在我`reconciliation_reason`它们的地位是`pending`, `delivered`,
`failed`, `manual_reconciliation_required`没有`discarded`试图历史和
只有在操作资源上才能找到调整证据.
资源继续返回 `provider: null` 和 `delivery: null`.

共有的 `provider` 封面是:

```json
{
  "kind": "zulip",
  "account_uuid": "account-uuid",
  "external_id": "provider-entity-id",
  "capabilities": {},
  "delivery_class": "live",
  "notification_eligible": true
}
```

`delivery_class`是 `live`或 `backfill`; `notification_eligible`是
在3.4节中描述的后端冷摄入决定 REST 和实时
完全快照的值相同.客户端压制桌面通知,
声音,并注意当它是明确的`false`;原生资源和
提供者信封在此选项字段存在之前生成的保留其
常规通知政策.

在转变提供商设计的流,主题或消息之前,后端
锁定其聊天/account映射并验证选择,活力,分配,
并且有效能力. 失败的预飞拒绝请求之前
圣经中的Messenger没有局部成功模式
暂停,离线或功能失调的服务提供者目标.Messenger
目标继续沿着现有路径.

### 5.4 外在身份

通过现有用户查找表面返回外部身份
只有读取的预测用户,具有明确的身份元数据:

```json
{
  "uuid": "stable-external-identity-uuid",
  "identity_kind": "external",
  "provider": { "kind": "zulip", "account_uuid": "account-uuid" },
  "display_name": "Provider user",
  "avatar": "urn:image:file-uuid"
}
```

没有解决的外部身份无法验证,
或用于打开一个IAM个人资料或本地个人流.
仅在后端之后,所有者身份由其现有的 IAM UUID 表示
验证了对该报告的认证 Zulip `(realm_uuid, user_id)`
电子邮件和显示名称的相等性从来没有触发这个链接.

### 5.5 实时事件和客户端缓存

现有的项目/user事件流获得了清洁的全快照事件:

- `external_account.created`, `external_account.updated`,
  `external_account.deleted` 对于所有者而言;
- 对于 `external_chat.created`, `external_chat.updated`, `external_chat.deleted`
  车主;
- `external_operation.created`, `external_operation.updated`,
  `external_operation.deleted` 对于所有者而言;
- 预测的普通流,主题,消息,用户,文件和读取事件
  Messenger实体

用户界面存储正常帐户,聊天,能力,提供商和操作
快照在IndexedDB中并更新它们从全快照事件.
或一个时代生成不匹配清除这些缓存,并执行新的 REST
在启用通知之前的快照.

## 区域管理 API 建议

已批准的管理形式将所需的策略与仅读策略分开
总体健康.IAM通过现有的自我检查提供许可
`permissions`列表,通常通过分配的角色,和Workspace
后端执行每个路线的特定操作权限.
名称遵循 `service.resource.action`; 角色名称从未被用作操作
流角色`administrator`也不足,因为它是
项目和流域范围.

| 方法 | 航线                                                       | IAM 许可                               | 目的                                                                            |
| ------ | ----------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------------------------------- |
| `GET`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.read`    | 阅读清洁领域政策,以获取账户类型.                                   |
| `PUT`  | `/external_provider_policies/{kind}`                        | `workspace.external_provider_policy.update`  | 使用 `If-Match` 更新类型特定策略;有效载荷是一个动态类型模型. |
| `GET`  | `/external_provider_health/{kind}`                          | `workspace.external_provider_health.read`    | 阅读汇总桥梁和账户状态的帐户类型.                      |
| `POST` | `/external_provider_policies/{kind}/actions/suspend/invoke` | `workspace.external_provider_policy.suspend` | 紧急暂停整个领域的账户.                                     |
| `POST` | `/external_provider_policies/{kind}/actions/resume/invoke`  | `workspace.external_provider_policy.resume`  | 在验证后恢复.                                                           |

为了全面的政策管理和综合健康可见性,
管理 IAM 角色,这些五个准确的权限:

- `workspace.external_provider_policy.read`
- `workspace.external_provider_policy.update`
- `workspace.external_provider_policy.suspend`
- `workspace.external_provider_policy.resume`
- `workspace.external_provider_health.read`

不允许 `workspace.external_provider_policy.*`: Workspace 和元素
显现使用只有精确操作权限,没有奇迹权限资源
提供了.

仅仅是健康总和计数和延迟/queue指标.
答案从来没有包含帐户电子邮件,服务器 URL,聊天名字,凭证,或
消息内容. 定制CA输入只接受CA证书,拒绝私有
关键是"版本".

运行时桥识别通过共同的顶级层面分别暴露
`/external_bridge_instances/`管理资源:

| 方法 | 航线                                                               | IAM 许可                               | 目的                                                                                      |
| ------ | ------------------------------------------------------------------- | -------------------------------------------- | -------------------------------------------------------------------------------------------- |
| `GET`  | `/external_bridge_instances/`                                       | `workspace.external_bridge_instance.read`    | 清理供应商类型之间的桥接实例列表.                                       |
| `GET`  | `/external_bridge_instances/{instance_uuid}`                        | `workspace.external_bridge_instance.read`    | 阅读身份生成,状态,能力,心跳,证书到期,安全错误. |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/suspend/invoke` | `workspace.external_bridge_instance.suspend` | 立即阻止身份,而不需要撤销其生成.                              |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/resume/invoke`  | `workspace.external_bridge_instance.resume`  | 恢复未被撤销的暂停身份.                                                     |
| `POST` | `/external_bridge_instances/{instance_uuid}/actions/revoke/invoke`  | `workspace.external_bridge_instance.revoke`  | 无可逆转地撤销活动证书生成.                                       |

资源从来没有返回证书,私人资料,入学秘密,
操作返回数据,并将其用于
更新了清洁实例快照. 提供者政策和总体健康停留
编写轮换是有意的
不是一个 Messenger API 操作:一个平台操作员旋转Exordos秘密
通过显示/CLI,Core将其管理节点配置交付给
后端和桥端,后端自动打开匹配
只有一个代人.Workspace后端没有接收Exordos核心凭证.

Workspace元素显示所有十五个规范行动的规定
上述许可资源和两个具有固定的许可的全球角色
设置. `workspace-external-integration` 包含六个拥有者范围的外部
帐户权限.`workspace-external-integration-admin`包含了九个
提供者政策,综合健康和桥接实例管理
操作员分配的操作权限.
项目在 Workspace 项目中明确发挥作用,直到 IAM.
删除这种限制将消除无需访问的权限.
改变用户的普通 Workspace 角色.

## 7. 私人控制飞机

浏览器从来没有直接调用桥梁或Zulip.
API 连接Workspace后端和桥梁.它承载了配置和
只有状态;它从来没有运输消息体或Messenger事件.

已批准的运行时面是一个独立的私人后端听器,
进程, `workspace-external-bridge-api`,其自己的版本`/v1/`根.
它只绑定平台内部界面,需要一个有效的客户端
证书在 TLS 插座,除了第一次注册,每条路线,并且是
没有公众 Workspace nginx 的代理.
通过此方法,将所有数据都被验证为真实 TLS
已有的 IAM 认证证书
听器保持不变,并没有暴露内部控制或文件路线.
控制和文件资源共享这个私人OpenAPI收听器,但仍然存在
单独的资源组.

听器 PKI 是后端所有,并存储在一个专门的小持续
磁盘的秘密连接到后端VM.
域约束控制CA,服务器密钥和证书,以及完整性元数据
后端根图像替换安装相同的磁盘,必须失败
关闭一个缺失,部分,不安全或不匹配的 PKI 商店,而不是
默默地产生一个新的信任根.

第一个控制-CA信任使用域绑定HMAC认证的启动
产品/consumer模式;它不使用TOFU或禁用TLS验证.
在打开 HTTPS 控制听器之前,
桥接调用一个单独的平台内部平面HTTP `GET /ca.crt`终点
具有新鲜的256位小写六十六进制数字 `nonce`,精确的预期控制
`hostname`, `bridge_instance_uuid`并且是积极的.`enrollment_generation`没有
其他身份字段允许多个安装领域只选择
要求未开放的代.一次性注册的秘密从来没有
后端返回公共控制CA字节,
`Content-Length`没有`X-Workspace-CA-HMAC-SHA256`.

两个同行都将 HMAC 密钥和持续注册验证器导出为
`SHA-256(b"workspace-bridge-enrollment-v1\0" + token_utf8)`答案是HMAC
涵盖了不同的协议背景
`workspace-external-bridge-control-ca-v1\0`没有,NUL接收器名NUL,
定制桥梁 UUID, NUL,没有前零的基-10生成, NUL,以及
确切的PEM桥接禁用重定向,强制现有
时间过时10秒,512字节请求目标限制,和1MB CA限制,比较
在常时的 HMAC,验证PEM与TLS解析器,并原子
在启用主机名验证之前,将其安装到文件和目录fsync
TLS. 获取公有CA不会消耗注册生成;
通过成功的 CSR 签名,

桥只在桥持久磁盘上创建客户端私钥
通过一次认证的注册流提交一个 CSR.
签署 CSR 持久控制 CA,只返回客户端
证书和公共CA链;桥私钥从来没有穿过
机器界限,启动式交付, HMAC 首次信任,发电消耗,
证书轮换是定义的
[`zulip_bridge_control_api_v1.yaml`](../zulip_bridge_control_api_v1.yaml).

登记使用每个桥的不同的Exordos秘密资源
程序生成随机启动材料并提供它
通过保护的核心管理节点配置.
后端只存在一个验证器和生成机密磁盘.
已经成功了.CSR签署
原子消耗这一代,所以重播陈旧的节点配置
没有获得其他证书. 桥状态失去了重新注册
需要一个明确的轮换Exordos秘密资源和一个新的
永久共享的注册秘密不支持.

每个mTLS客户端身份都代表一个桥梁安装和一个
提供者类型.它的证书是绑定到 `realm_uuid`, `provider_kind`,和
`bridge_instance_uuid`;证书要求的确切编码是定义的
内部OpenAPI安全合同. Zulip桥架实例使用该身份
对于所有分配给它的 Zulip 帐户.后端仍然授权每个
现在的预期任务,因此拥有一个
仅仅是有效的桥梁证书就不会允许访问任意账户.
证书不按外部帐户发行或供应商共享
它们是这样的.

服务器和客户端页证书有效30天,并自动开始
桥接生成一个新的私钥,
CSR 本地,并使用其仍然有效的mTLS身份认证更新;
后端只签署一个带有相同的认证身份声明的证书.
旧和新客户证书重叠最多24小时,允许
已过期的客户端证书不能使用
需要一个明确的招生秘密轮换.
后端或桥图像更新不会自行旋转证书.

控制CA有效期为五年,只通过明确的,
轮换创造了新的CA在
并且发布一个双信任包30天.
通过其身份验证,在新CA下获得一个新页.
旧有效身份.旧CA仅在每个活跃桥之后退休
程序将显示一个重叠窗口.
移动状态和失败关闭而不是默默地延长过期的
基于信任的根.自动的年度CA更换和安装寿命CA
没有支持.

后端对每个桥识别的活跃证书具有权威性
控制和文件请求检查这些值后
TLS资源授权之前,包括对一个
暂停一个身份,立即阻止它,可能在以后
恢复同一个代号. 撤销一个身份是不可逆的:后端
推进自己的世代,拒绝旧一代的证书,
并且需要一个轮换的入学秘密加上一个新的CSR. 单独的叶子到期是
不是撤销机制,没有听器重载或CRL传播
需要.

已批准的控制方向是由 Workspace 拥有的内部 API
桥梁拉出所需的代号和加密凭证
提交心跳,能力,进展,以及实际状态.
调整是周期性的,不依赖于单一的 RPC
取得成功.

想要的状态同步使用版本增量变更源
`GET /v1/desired-state/changes`它的不透明光标与世界相连,
提供者类型,桥接实例,过器集和控制方案版本.
答案包含一个顺序的同样有效的批量和下一个检查点.
桥接将一个批量交易应用到其运营数据库中,并且只
并且在事故发生后, 继续进行重播.

每个 `external_chat_assignment` 完全更换包括一个后端所有
`workspace_projection`它们包含了流量.UUID并且呈现,
参与者提供商ID与Workspace身份 UUID映射,以及提供商主题
标识符映射到 Workspace 主题 UUID.桥梁持续使用此映射;
它从来没有发明Workspace流,主题,参与者或消息UUID.
另外,该任务也将供应商歧视者
`provider_chat.kind`;它是完全的正规替代的一部分,而
图表摄入接受个人直接的
只有与两个不同的参与者聊天,只有一个群体直接聊天
否则,我们会在聊天或交谈之前拒绝.
一个 `external_chat_assignment` 想要的资源是持久的.
提供者发现报告 topology 没有 Workspace UUID,后端
在发布分配之前分配稳定的 UUID.
在提供者支持的流预先并发布分配生成
随着新主题映射,但没有排队 `topic.upsert`: Zulip 实现
第一个 `message.create` 的主题. `topic.upsert` 专为
转名一个已经有提供者消息映射的主题.
第一个出发消息工作时`history_depth=new`没有入境历史记录
已经实现了地图.

增加的增值和全快照资源都具有有效的效果.
`required_capabilities`在写出所需状态之前,实现投影
桥梁验证每一个要求和
需要资源类型, UUID,和生成匹配增量
任何不匹配都会导致整个批量关闭.

如果一批包含未知 `resource_type`,未知 `operation`,或一项
需要在谈判交叉口之外的能力,
滚回整个批量,而不推进它的光标.
安全不兼容性报告,后端标记桥实例
`incompatible`.桥不能跳过或隔离违反的项目和
不应从该批次中订购后续的货物;在恢复兼容性后,
同样的批次从未改变的光标重新播放.

随着一个实例的 `incompatible` 时,心跳仍然可用.
有效的心跳广告功能,覆盖封锁批量,
后端自动清除`incompatible`,桥接重播该批量
没有任何变化.
区域管理员`resume`没有一个完全的快照.
管理性暂停或撤销证书.

后面的 V1 桥使用普通的投票,而不是长期的投票.
变更料响应它等待两个秒钟,并发出下一个请求
只有一个投票可以在每一个桥实例中,
没有变化的检查点即刻返回空应答.

在网络故障后, HTTP `429`,或可重新试用 `5xx`,投票使用
指数式反转与全震动:一个秒的底是30秒的
. HTTP `429` 和 `503` 荣誉 `Retry-After` 长达五分钟.
导航器从不在失败时进行进度,心跳发射有自己的重试
循环.第一个成功的料响应重置后置和恢复正常的
两秒间隔.

每个变化都是一个包含 `change_uuid`,单调的替代记录
序列, `resource_type`, `resource_uuid`, `operation` 和资源
生成.一个`upsert`载有所需资源的完整快照
包含仅加密的凭证信封和其他桥接授权的信封
应用相同或较老一代是无选择的;
取代本地资源原子. A `delete` 仅携带一个墓碑
资源的身份和生成,从来没有删除的秘密或先前的有效载荷.
没有使用JSON补丁和获取后更改记录.

完全恢复开始于`POST /v1/desired-state/snapshots`.
逻辑快照会话,并返回一个不透明的快照令牌,一个变更
标记器,`snapshot_generation`快照页面使用一个
页面的光和稳定的 `(resource_type, uuid)` 顺序.
不持有PostgreSQL交易,并且不会在会话的整个时间内进行交易.
将完整的快照作为一个应用内存或 JSONB 阵列.
快照资源被结为正常,顺序 PostgreSQL 行和每一个
HTTP页面最多读取请求的限制加上一个前行.
后端内存限制当数万项任务包含大
参与者和主题目录.
它们在这些冷的行或在严格的后代料中
桥安装所有页面,然后重新播放更改后的
,并承诺的状态加上检查点原子.
快照令牌需要启动新会话.

已过期,不匹配或不再可解码的光标返回
显式重置响应,而不是默默跳过更改.
载入一个一致的页面化完整的想要状态快照,并安装
接收后再加上其检查点,然后再开始增量输送.
根据资源 ETag 调查和后端发起的 WebSocket 交付不是其中的一部分
对于v1

恢复响应是 HTTP `410`
`type=ControlCursorExpiredError`, `error=control_cursor_pruned`一个打字
对于 `retention`, `generation_mismatch`, `scope_mismatch` 的 `reason`,或者
`schema_mismatch`现在的电流`snapshot_generation`这包括
`Cache-Control: no-store`. 这反映了公众Messenger光标间隙
通过使用私人光标,将合同保持不透明. HTTP `409` 仍然可用.
对于普通状态/precondition冲突,并且重置从未被编码为
没有一个成功的空批量.

控制飞机变更记录保留了7天,然后
仅适用于增量日志:当前
外部账户,加密凭证信封,聊天分配,提供商
它们的权威模式.
桥梁在线时间不超过7天,通常可以逐步赶上;
一个较旧的光标使用相同的全快照恢复路径.

桥返回通过观察状态
`POST /v1/observed-state/reports` 批量最多500件.
具有客户端生成的 `report_uuid`,资源身份,观察到的想要
代,状态,进展,以及一个有界限的安全错误.
`report_uuid`是一个无选择的,一个陈旧的观察代不能覆盖一个
最新后端记录.后端在响应之前仍然接受报告,
所以如果失去了响应,再试一次.

提供者发现使用类型为 `external_chat_catalog` 的观察资源.
每个目录项目是一个`upsert`或`delete`墓碑与当前的
外部帐户生成,并承载帐户,所有者,提供商,以及
后端将使用一个简单的方法来处理.
拒绝所有权或生成不匹配,保持稳定的 Workspace 聊天
UUID对于每个提供者聊天键,并不断分配新的项目,当
帐户使用 `selection_mode=all`.

报告批次允许部分接受.成功的 HTTP `200`响应
每个 `report_uuid` 请求顺序中的一个结果,状态为 `applied`,
`duplicate`,`stale`,或`rejected`和可选的边界安全错误.
项目不阻止有效的独立项目. mTLS故障或不有效批量
封面/schema返回适当的`4xx`,并没有应用任何东西.
仅删除 `applied`, `duplicate`,并故意丢弃 `stale`
报告从其耐用出口箱;一个可重新测试的 `rejected` 项目仍然在排队中.

活力和能力使用单独的轻量级
`PUT /v1/bridge-instances/self/heartbeat` 终点. 心跳发射没有
依赖于账户/chat工作可用,从来没有承载每账户
它们的数据,信任或信息数据.
后端接收时间是权威的; 30 后桥变成 `degraded`
没有心跳的秒钟和60秒后的`offline`.
只有诊断. 否则,以后有效的心跳恢复健康.
身份被行政暂停或撤销.

每一个心跳都在其`/v1`URL和URL选择的API大调下运行
声明证书绑定的提供者类型,命名能力,以及相关的
没有单独的控制方案谈判.
名称包括聊天目录,发送消息/edit/delete/read,会员写,
后端计算了失败的关闭交叉点
并且只发出所需的资源和操作支持该实例.
需要缺失能力的任务变成`unsupported_capability`
后端从来没有尝试过乐观的交付.
并且不作为一种替代能力.

心跳线表示是一个 JSON 对象,由稳定的名字间隔键
能力名称.每个值是一个包含能力的描述符
`revision` 和一个特定能力的 `limits` 对象,例如
`{"messenger.message.edit": {"revision": 1, "limits": {}}}`.后端
仅考虑已知的名称,并交叉数值或编号的限制
失败关闭. 无知能力名称被忽略而不是作为证据
操作是否得到支持.

一个能力 `revision` 是一个正面的,单调的向后兼容
后端要求声明 `min_revision`;一个桥描述符是
如果其修订等于或超过该最低值,则是相容的,但
对于一个高级的修改必须保留所有
通过更低的修订承诺的行为. 破坏语义变更使用一个新的
能力名称;现有的修订永远不会重新定义或重复使用.

私有 API 只有其 URL (`/v1`) 中的主要组件版本;
没有次要版本谈判.客户端必须忽略未知 JSON 对象
需要在一个单独的领域中使用,以便独立地推出增量反应元数据.
行为通过命名能力交叉点明确谈判,
没有从图像版本或隐含的小方案推断.
字段的意思和类型不能在 `/v1` 中改变;删除一个字段或做一个
选择性领域需要一个新的API专业.

拥有Workspace的内部资源:

- 想要的帐户生成和清理设置;
- 密码的凭证信封,与帐户 UUID,领域 UUID,算法相关,
  关键版本,以及相关数据;
- 选择的聊天/project任务和历史记录深度;
- 版本化自定义CA捆绑;
- 桥梁能力,心跳,进展情况和安全错误报告;
- 命令 UUID 和所需生成的idempotent确认.

桥梁通过其网络发布其领域密码公钥
只有桥接持久磁盘保留
相关私有主密钥; Workspace PostgreSQL 仅存储加密
桥只在需要调用时本地解密凭证
提供者. 纯文本凭证从来没有出现在控制响应中,
Workspace日志,或桥接持久运行表.

确切的内部 OpenAPI 作为一个单独的合同文物.
包含mTLS身份,重播保护,生成单调性,请求
限制,错误语义,并被实施的合同
测试.

## 8. 消息和事件数据平面

提供者数据平面是一个私人桥 autenticated HTTP API 根在
`/api/workspace-provider/v1`. PostgreSQL 是正规的 Messenger 存储,
请求所有RESTAlchemy事务是唯一的提交边界. IMAP, SMTP,
邮箱,和 MIME 消息不是提供商同步的一部分.

- 后端到桥梁操作是租来的.FIFO订单从一个持久的
  PostgreSQL队列. 过期的租是可收回的和独立的桥梁
  工人使用`SKIP LOCKED`而不重复索赔.
- 桥接到后端事件作为有限的原子批量提交.
  提供者范围从mTLS身份检查,事件 UUID
  并且一个被拒绝的项目将推迟
  完全的批量.
- 创建,更新,删除和未读无效事件使用
  观众快照和有限的广播行.事件行增长是有限的
  通过逻辑突变和受影响的实体而不是流成员.
- Workspace 提供商支持的频道流排队的约束性变化
  能力关闭的 `membership.add` 和 `membership.remove` 操作.
  Zulip桥解决了映射的身份,并使用官方订阅
  API. 这些突变是普通的持久,安全的聊天通道操作;
  源链接从未进入提供者队列.
- 选择的频道的参与者预测为 `ready` 成为符合条件的
  对于一个受限的参与者,在30秒后重新检查.
  订户集通过现有目录报告/desired-state
  手握,更新 Workspace 身份和绑定在行之前
  返回 `ready`.
- 终端提供商的结果是批次,由 `result_uuid` 进行 idempotent,并返回
  每项申请一个状态. 过时租不能完成重新租的工作.
- 操作 UUID,提供者帐户 UUID,提供者实体ID,提供者修改
  提供者中立的身份和订单数据.
  提供商的原料负载仍然是内部的.

租需要现有的控制飞机心跳是健康的,没有
已知操作类型只有当当前的
他们的心跳广告他们的名字能力.
操作的 `PUT /v1/bridge-instances/self/heartbeat` 并没有重复
数据平面 API.

订单使用外部帐户和聊天范围的因果线路/entity.
一个实体的冲突操作是串行执行的,而独立的聊天
桥梁只有在
提供者确认其提交. Workspace,提供者 HTTP,控制,和桥梁-本地跳跃
通过一个操作来删除重复.UUID. Zulip消息创建由
提供者特定的调整政策下面,因为 Zulip 不使用
客户端 `local_id` 作为一个身份密钥.

在一个模糊的 Zulip 发送结果后,桥首先执行延迟,
查询到目标对话的确切内容.
最新的先,缩小到当前的外部帐户作为发送者,原始请求
标记,并比较目标,可规的有效载荷,附件,和边界
操作时间窗口. 一个或多个准确匹配确认原始发送;
桥选择最接近第一次发送尝试的候选人,打破联系
通过最低的数字提供商消息ID,并不会再发送.
只有作为调和证据.
故意处理同一个帐户中的相同消息
两个用户操作可以进行,
它们的总体数量是1Zulip如果重复检查发现没有匹配,桥梁可能会
仅一次自动重发. 历史记录不可用或第二个模糊
结果需要 `manual_reconciliation_required`;没有进一步的自动重新发送
允许使用.

具体的电线合同是
[`workspace_provider_api_v1.yaml`](../workspace_provider_api_v1.yaml).

## 9. 双向文件传输飞机

桥不能使用 MIME 附件,也不能接收桶宽 S3
已批准的二进制转移边界是一个独立的内部mTLS
文件 API 具有短暂的单个对象预标 URL.

接入供应商文件流:

1. 桥请求与外部帐户 UUID,聊天 UUID,
   操作 UUID,名称,大小,内容类型和预期的哈希值.
2. 后端验证分配并返回一个短暂的预标 URL
   这允许`PUT` 完全一个悬而未决的对象.
3. 桥上传字节,并完成调用.
4. 后端验证大小和哈希,原子化创建JSON侧车和
   现在的 ACL,提交文件投影,并返回一个 Workspace URN.

没有人会说Workspace文件流量:

1. 桥接提交授权的 Workspace URN,外部帐户 UUID,
   聊天 UUID,操作 UUID.
2. 后端重新计算了来自聊天/stream任务的当前访问和
   返回对该对象的短暂预定 `GET` URL.
3. 桥接下载字节并将其复制到提供商存储中.

桥不能创建或修改侧车和ACL,列出桶,或重复使用
a URL后端清除过期的部分分配和
当一个投影被删除时,立即清除提供商所有的复制文件.

预标注的 `PUT` 和 `GET` URL 在五分钟后会过期.
完成需要声明的大小,内容类型,以及SHA-256;完成失败
任何不匹配情况,都会关闭./finalize它们对相同的
已过期未完成的分配被删除
后台清理工作者.

这个服务只允许二进制文件字节;字节直接流动到
桥梁和对象存储. 消息文本,元数据和事件使用
隐私提供者 HTTP API,结果的消息只包含返回的
URN.

## 10. 存储和部署的边界

### Workspace后端

- 通过一个新的域范围的外部帐户,加密的凭证,聊天分配,
  提供者映射,所需/actual状态,以及外部操作预测.
- 稳定的领域/installation UUID. 因为一个后端数据库属于一个
  唯一性是 `(owner_user_uuid, provider_kind)` 在数据库中.
- 一个项目调动协调员,撰写了经典的旧/new项目日记
  通过保存实体 UUID 进行过渡.
- IAM生命周期调整, 除了目前的惰用户发现.
- 隐私的mTLS控制,提供者数据和文件终端.
- 公共序列化器,可以保存`provider`和`delivery`的流,
  其他主题,消息,文件和次要预测.

### `workspace-zulip-bridge` 元素

桥是一个新的存储库,
一个可更换的根盘,一个持久的数据盘.
路线已经暴露.

持续状态包括:

- 域密码密钥版本和控制平面身份;
- 持续的出口箱/inbox除重复和提供商 HTTP租状态;
- Zulip 队列标识符和标记符;
- 提供者对Workspace的映射和后填进展;
- 时间安排租和实际情况报告.

对于目标用户来说,
信息,并100条信息/second,运营存储应该是一个本地
持续磁盘上的崩安全 PostgreSQL 实例.
规范性 Workspace PostgreSQL 和 S3 状态.

桥使用一个公平的时间表, 严格的现场优先, 然后可重新试用
后面是每个人账户的公平补充.Zulip利率限制是有权的.

### 安全更新

- Workspace它们没有被改造,
  在工作装置上卸载或重新部署.
- 每个开发图像都使用一个新的不可变的版本.
- 持久磁盘身份,节点导出,磁盘顺序和数据标签仍然存在
  通过升级稳定.
- 启动失败关闭缺失/partial关键状态或领域不匹配.
- 排队,映射,以及本地数据库必须在硬机停止后恢复,
  因为当前的核心图像更换不保证优雅的关闭.
- 激活是分阶段的,并具有能力门:后端API和桥梁
  提供者提供残疾类型,然后招生,健康的心跳,
  并且在一个域管理员之前验证所需的能力交叉点
  启用 Zulip.
- 滚回将暂停提供者同步,并保留持久队列和
  恢复的投影. 它不卸载一个元素,删除持续
  状态,或中断原生 Workspace 消息.

## 11. 工业界限

- 取代隐藏的遗留外部帐户功能;不要使其可见或
  添加其旧终端的兼容性调用.
- 添加一个 Messenger 设置页面,用于 Zulip 凭证,聊天选择或 `all`,
  历史深度,项目分配,进展, `Disconnect`,以及破坏性
  `Delete`.
- 通过流,主题,消息,文件和缓存保存提供商元数据
  其他预测.
- 使用紧的交互式提供商章与帐户/status饼和一个
  如果可以,原始链接.
- 集中在编曲器,编辑/delete,文件,
  改名/move,回复,并失去了飞行前.
- 保持本地浏览器运输状态 (`sending`/`failed`/`sent`) 与
  权威的外部操作状态 (`pending`/`delivered`/`failed`).
- 永远不要将提供者凭证发送到IndexedDB,日志,分析或桥梁
  通过浏览器.

## 12. 技术分解

1. **合同基础**:批准这个公共边界,领域管理
   授权原始,私人控制和提供者数据 OpenAPI,以及
   加密封面的方案.
2. **Workspace数据模型**:替换旧的项目范围帐户残余;
   实现加密凭证,分配,映射,操作状态,
   IAM生命周期,以及项目调动协调.
3. **Workspace协议边界**:添加mTLS提供商身份,持久
   提供商 HTTP 发行箱/ingress,内部控制/file服务,出口
   桥梁元素所要求的.
4. **投影和实时平衡**:填写提供者/delivery元数据,
   外部身份,全快照事件,缓存重置行为,以及
   通知门
5. **桥元素基础**:创建单独的存储库,显示,
   持续启动,安全运行 PostgreSQL,mTLS注册,
   并且调和循环.
6. **Zulip连接器**:帐户验证,目录,现场队列,持久的出口箱,
   最新的第一回填,转换,冲突规则,以及利率限制意识的公平
   时间安排.
7. **UI**:标准化缓存第一域,连接向导,选择/project
   流量,章/popovers,外部身份,能力,飞行前,以及
   试试/discard行为
8. **接受**:合同测试,实际提供商HTTP和Zulip的集成,
   文件 ACL 测试,崩/root-replacement恢复,负载测试,安全元件
   剧作家的完全可见的接受.

## 13. 接受矩阵

没有验证所有以下内容之前,该特征就不会完成:

- 创建帐户/reconnect/disconnect/delete和 IAM禁用/delete;
- 每个提供商的单一帐户和仅可写入的凭证;
- 显式选择,动态 `all`,所有历史深度模式,以及新聊天
  任务
- 频道/topic,个人DM和组DM映射;
- 创建/edit/delete/read/mentions/replies/quotes/Markdown/links/files/images
  在两方向;
- 在功能广告时,将其两向重命名;
- 损失回归,原始链接和出口确认;
- 最新的第一次填补,第一时间安排,通知门口和p95
  延迟目标;
- 重新尝试/backoff,24小时到期,丢弃,崩恢复,除重复,以及
  冲突/delete 排序;
- 项目移动稳定的 UUID/history/read状态和正确的旧/new项目
  活动
- 删除选项/access-loss 立即清理投影和复制文件;
- 提供者元数据的平等性 REST,实时,IndexedDB和次要视图;
- 没有IAM冒充的外部身份和`urn:user:*`行为;
- 定制CA验证,主机名验证,mTLS控制,最少特权
  提供者 API访问,以及没有全球 S3访问;
- 仅限所有者详细信息和仅限总体区域管理员健康信息;
- 每秒100条消息
  负载配置;
- 保持持续状态的安全独立桥梁更新;
- 通过正常的 `cassi` Workspace 账户可见的剧作家接受.

## 14. 改版的合同文物和剩余的门

高级产品和API界限已批准.
控制 OpenAPI,提供者 HTTP OpenAPI 和内部文件 API 是实现
需要在执行之前进行合同,兼容性和运行时间验证的输入
让我们来看看.

这些完成的文物是实现输入,而不是剩余的发布
现在,我们需要一个新的系统,
[`zulip_bridge_v1_test_plan.md`](zulip_bridge_v1_test_plan.md)包括
具体的政策授权,实际双向 Workspace 提供商 HTTP/file
和 Zulip 12.1.1 交通,证书轮换,恢复和目标负载
查看,并完全可见的剧作家接受.
