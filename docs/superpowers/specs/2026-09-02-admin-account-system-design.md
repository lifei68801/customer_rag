# 管理后台账号体系设计

> 日期：2026-09-02
> 状态：设计定稿，待转实现计划
> 范围：管理后台（`/admin/*` 与 `/api/admin/*`）。前台问答界面不在本次范围内。

## 1. 问题

当前管理后台的鉴权有三个事实：

1. **没有用户名。** 登录只提交一个 `CUSTOMER_RAG_ADMIN_TOKEN`（环境变量），换回一个 session token（`app/api/admin_auth_routes.py:26`）。没有用户表，没有密码哈希，没有身份概念。
2. **session 存在进程内存里**（`app/api/admin_session.py`），后端重启全员掉线。这一点本次不改。
3. **租户隔离完全不存在。** `require_admin_session` 只校验 session 有效，**从不校验登录者与被操作租户的关系**。任何登录者把请求里的 `tenant_id` 换成别的值，就能读写另一个租户的数据——请求返回 200，没有任何日志或报错。

第 3 条是本设计要解决的核心问题。第 1 条是达成它的手段：没有身份，就无从谈"这个人属于哪个租户"。

租户 id 目前有**四种**传法，这是隔离难以落实的直接原因：

| 来源 | 路由 |
| --- | --- |
| 路径参数 | `/api/admin/{tenant_id}/terms`、`/{tenant_id}/diagnostics`、`/{tenant_id}/schema-etl`、`/api/admin/ontology/{tenant_id}/…` |
| 查询参数 | `/api/admin/documents`、`/api/admin/nav-badges`、`/api/admin/graph-reviews`、`/api/admin/duplicate-reviews` |
| 表单字段 | `POST /api/admin/documents`（`tenant_id: str = Form(...)`） |
| JSON body | `POST /api/admin/graph-reviews/{id}/approve`、`/reject`，duplicate-reviews 同 |

一个公共依赖无法同时覆盖这四处（Form 需要 multipart 解析，混进公共依赖会破坏非 multipart 请求），而按来源各写一个依赖，就等于留下四条路径——新增路由时挂错或漏挂都不会报错。

## 2. 设计决策

| # | 决定 |
| --- | --- |
| 1 | 引入多账号，账号绑定租户；`admin` 是不属于任何租户的超管 |
| 2 | 账号:租户 = **N:1**——一个账号恰属一个租户，一个租户可有多个账号 |
| 3 | 租户切换器**只对 `admin` 渲染**；`admin` 切入租户后可写 |
| 4 | `CUSTOMER_RAG_ADMIN_TOKEN` 降级为**首次播种**用的初始密码，之后可在界面改 |
| 5 | 「账号」页由 `admin` 管理；账号**只禁用不删除**；不能禁用最后一个可用 admin |
| 6 | `hashlib.scrypt`（不引入新依赖）+ 自描述存储格式；登录限流 5 次 / 15 分钟 |
| 7 | 有状态 session 承载 `{username, role, tenant_id}`；父 router 强制 + 结构测试兜底 |
| 8 | 迁移只播种 `admin`，**不自动创建普通账号**；禁用 6 个测试残留租户 |
| 9 | 左下角两行（租户名 / 用户名），菜单项按角色分；普通账号可改自己的密码 |
| 10 | 一次性替换，不保留旧登录路径；播种失败让**进程启动失败** |
| 11 | 先把租户作用域路由**统一成路径参数**，再上第 7 条的两条防线 |

### 逐条理由

**决定 2（N:1）**：严格 1:1 会迫使同一客户的多个人共用一个密码，「这批数据是谁批准的」就永远无解——而这个系统里删文档、批准关系入 Neo4j 都不可逆。M:N 则要求切换器回归，与需求冲突。

**决定 3（切换器只对 admin）**：需求的实质是"普通账号不能跨租户"，这在后端是硬约束。对 `admin` 保留切换是权限的自然表达。让 `admin` 只读的替代方案被否决：排查到一半改不了，人只会去要客户的密码，绕过整个设计。

**决定 4（播种）**：让 `admin` 与普通账号走**同一条**校验路径。若保留 token 明文比较作为 admin 的专属路径，则限流、密码策略、最后登录时间每样都要写两遍，两遍里总有一遍会忘。副作用是环境变量与实际密码会在改密后不一致——必须在 `.env.example` 与 README 中写明「仅用于首次播种」。

**决定 6（scrypt）**：`hashlib.scrypt` 是标准库（OpenSSL 实现，RFC 7914），不需要编译依赖。`bcrypt` / `argon2-cffi` 都需要 cffi 或 Build Tools，而本项目在 Windows 上开发。自描述存储格式让将来换算法/调参数不必迁移历史密码。

登录限流是**必需项**而非增强：原凭证是 32 字节随机 token，爆破不现实；换成人选的密码后熵急剧下降，而 `admin_auth_routes.py` 现有注释已经承认"目前没有限流/锁定"。

**决定 10（一次性替换）**：双轨或功能开关意味着旧的越权路径仍然活着，且没人记得它还在。本项目是单机部署，没有客户端过渡期的压力。

**决定 11（统一寻址）**：见第 1 节。统一后所有租户路由形状一致，第 7 条的两条防线才真正兜得住。

## 3. 阶段一：统一租户寻址

先做、可独立验证、不涉及账号体系。

### 3.1 路由改名

| 现路径 | 新路径 |
| --- | --- |
| `/api/admin/documents…` | `/api/admin/{tenant_id}/documents…` |
| `/api/admin/graph-reviews…` | `/api/admin/{tenant_id}/graph-reviews…` |
| `/api/admin/duplicate-reviews…` | `/api/admin/{tenant_id}/duplicate-reviews…` |
| `/api/admin/nav-badges` | `/api/admin/{tenant_id}/nav-badges` |

`tenant_id` 从查询参数 / `Form(...)` / JSON body 字段中**移除**，改由路径参数提供。请求体模型里的 `tenant_id` 字段一并删除。

**不改动**（`tenant_id` 已在路径中，防线按"路径含 `{tenant_id}`"判定，与其所处段位无关）：

- `/api/admin/{tenant_id}/terms…`
- `/api/admin/{tenant_id}/diagnostics…`
- `/api/admin/{tenant_id}/schema-etl…`
- `/api/admin/ontology/{tenant_id}/…`

**非租户作用域**（不得挂租户校验，否则 FastAPI 会把 `tenant_id` 当成必填查询参数而返回 422）：

- `/api/admin/auth/*`
- `/api/admin/tenants*`
- `/api/admin/accounts*`（阶段二新增）

### 3.2 前端同步改动

| 文件 | 调用点 |
| --- | --- |
| `frontend/src/admin/DocumentsPage.tsx` | 7 处（列表、上传、删除、重试、删任务、chunks、下载） |
| `frontend/src/admin/GraphReviewsPage.tsx` | 6 处（待审列表、历史、批准、拒绝、批量批准、批量拒绝） |
| `frontend/src/admin/DuplicateTermSuggestionsTab.tsx` | 3 处 |
| `frontend/src/admin/useNavBadges.ts` | 1 处 |

上传是 `multipart/form-data`，去掉 `tenant_id` 字段后其余字段保持不变。

### 3.3 阶段一验收

- 后端既有测试全绿（涉及这些路由的测试需同步改 URL）。
- 前端 `npm test`、`tsc --noEmit`、`vite build` 全绿。
- 手工验证：文档上传、关系批准、疑似重复处理、侧边栏徽标各走通一次。

## 4. 阶段二：账号体系

### 4.1 数据模型

新表建在**本体库**（`settings.graph_review_db_path`）——`tenants` 表在那里，账号与租户的关联不应跨库。

```sql
CREATE TABLE IF NOT EXISTS admin_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    -- admin 不属于任何租户，member 必须属于一个
    tenant_id     TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    CHECK (
        (role = 'admin'  AND tenant_id IS NULL) OR
        (role = 'member' AND tenant_id IS NOT NULL)
    )
);
```

表名用 `admin_users` 而不是 `users`：前台问答有自己的 `user_id`（前端生成的 UUID，见 `app/api/session_routes.py`），两者是完全不同的东西，同名会让人以为它们相关。

`tenant_id` 不加外键约束——SQLite 默认不强制外键，加了给人以为强制了的错觉。租户存在性在应用层（创建账号时）校验。

### 4.2 密码哈希

```
存储格式：scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>
默认参数：n=16384 (2^14), r=8, p=1，salt 16 字节
```

- 哈希：`hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)`
- 校验：解析存储串取出参数与 salt，重算后用 `secrets.compare_digest` 比较
- 参数从存储串读取而非从常量读取——将来调参数时，旧密码仍能校验通过，下次改密自动升级到新参数

密码长度下限 8 字符，无字符类型要求。上限 1024 字符（scrypt 对超长输入没有保护，防止拿超长密码做 CPU 消耗攻击）。

### 4.3 登录与限流

```
POST /api/admin/auth/login
  请求  { "username": "...", "password": "..." }
  成功  200 { "session_token": "...", "username": "...", "role": "admin|member", "tenant_id": "..." | null }
  失败  401 { "detail": "用户名或密码不正确" }
  锁定  429 { "detail": "登录失败次数过多，请 N 分钟后再试" }
```

失败响应**不区分**"用户不存在"、"密码错误"、"账号已禁用"三种情况，一律同一条文案与同一个状态码——否则这个接口就成了用户名枚举器。三种情况在**服务端日志**里分别记录（不记录尝试的密码）。

限流状态存进程内存（与 session 同一形态）：`username → (连续失败次数, 锁定至)`。连续失败 5 次锁定 15 分钟，成功登录清零。重启清空锁定状态是可接受的——攻击者控制不了服务端重启。

按 username 计数而非按 IP：本系统部署在内网，IP 区分度低；且按 username 锁定不会让一个攻击者顺带把所有人锁在门外。

**已知局限**：不存在的用户名不占用计数槽位（否则内存会被任意用户名撑爆），这意味着攻击者可以用不同用户名无限尝试。这在只有个位数账号的内网系统里是可接受的——真正的账号仍受 5 次限制保护。

登录成功时更新 `last_login_at`。

### 4.4 Session

`AdminSessionStore` 从 `token → 过期时间戳` 扩为 `token → AdminSession`：

```python
@dataclass(frozen=True)
class AdminSession:
    username: str
    role: str          # "admin" | "member"
    tenant_id: str | None
    expires_at: float
```

不改用 JWT：JWT 签发后无法撤销，「禁用账号」将无法立即生效（决定 5 要求它生效），而维护黑名单等于又回到有状态，白付签名验签的复杂度。

`require_admin_session` 的返回值从 `None` 改为 `AdminSession`，供下游依赖使用。

被禁用的账号，其已存在的 session 在下次请求时失效——`require_admin_session` 除校验 session 有效外，还需确认该 username 当前仍是 `active`。这是决定 5「禁用立即生效」的落点。

代价是每个 `/api/admin/*` 请求多一次 SQLite 查询。这在本地文件库 + 管理后台的低请求量下可以忽略；不接受这个代价的替代方案是"禁用时主动撤销该用户的所有 session"，但那要求 session store 支持按 username 反查，且**只对进程内已知的 session 有效**——多进程部署时另一个进程里的 session 撤销不掉，会退化成静默失效。查库是唯一在各种部署形态下都成立的做法。

### 4.5 租户访问控制

```python
async def require_tenant_access(
    tenant_id: str,
    session: AdminSession = Depends(require_admin_session),
) -> str:
    """校验登录者有权操作 URL 里的这个租户。

    admin（tenant_id 为 None）放行任意租户；member 只能操作自己的租户。
    """
    if session.role == "admin":
        return tenant_id
    if session.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="无权访问该租户")
    return tenant_id
```

**防线一：挂载层强制。** 所有租户作用域 router 收进一个带该依赖的父 router，而非各挂各的：

```python
tenant_scoped = APIRouter(dependencies=[Depends(require_tenant_access)])
tenant_scoped.include_router(admin_terms_router)
tenant_scoped.include_router(admin_document_router)
# … 其余租户作用域 router
app.include_router(tenant_scoped)
```

**防线二：结构测试兜底。** 遍历 `app.routes`：

- 凡路径含 `{tenant_id}` 的路由，其依赖链中**必须**有 `require_tenant_access`
- 凡 `/api/admin/*` 下路径**不含** `{tenant_id}` 的路由，其依赖链中**必须没有**它（挂了会导致 422）

这条测试的价值在于新增路由时忘记归类会立刻变红。人的记性在第 9 个 router 上一定会失效，而漏掉的那条不会有任何运行时报错。

### 4.6 账号管理 API

```
GET    /api/admin/accounts                      仅 admin。列出全部账号
POST   /api/admin/accounts                      仅 admin。{username, password, tenant_id}
POST   /api/admin/accounts/{username}/disable    仅 admin
POST   /api/admin/accounts/{username}/enable     仅 admin
PUT    /api/admin/accounts/{username}/password   仅 admin，重置他人密码，不需旧密码
PUT    /api/admin/auth/password                  任何登录者，改自己的密码，需旧密码
```

`GET /api/admin/accounts` 返回 `username / role / tenant_id / status / created_at / last_login_at`，**不返回** `password_hash`。

新增依赖 `require_admin_role`（在 `require_admin_session` 之上再判 `role == "admin"`），挂在 accounts router 与 tenants router 上——新建/禁用租户同样只有 admin 能做。

**不变量**（服务端强制，返回 400）：

1. 不能禁用自己
2. 不能禁用最后一个 `active` 的 admin。在本设计下（只有一个 admin，且不提供创建 admin 的入口）它是不变量 1 的子集，**有意保留**：将来若开放多 admin，这条不必重新想起来
3. 创建账号时 `tenant_id` 必须存在于 `tenants` 表且状态为 `active`
4. 创建的账号 role 恒为 `member`——本设计不提供"再造一个 admin"的入口。多 admin 的需求出现时再单独设计，现在开这个口子会让不变量 2 变复杂而收益为零

用户名规则：3–32 字符，`[a-zA-Z0-9_-]`。`admin` 是保留名。

### 4.7 前端

**登录页**（`LoginPage.tsx`）：单一「管理员 token」字段 → 用户名 + 密码两字段。`autoComplete` 分别用 `username` / `current-password`。

**`useAdminAuth`**：登录响应中的 `username / role / tenant_id` 一并存入 `sessionStorage`（与 session token 同生命周期）。新增返回值 `role`、`username`、`boundTenantId`。

**`TenantContext`**：`tenantId` 的来源改为——

- `role === 'member'`：**恒等于** session 里的 `tenant_id`，`setTenantId` 变成 no-op（不是隐藏按钮，而是这个能力不存在）
- `role === 'admin'`：维持现有行为（sessionStorage 记忆 + 可切换）

**`AccountMenu`**：

| 项 | member | admin |
| --- | --- | --- |
| 触发按钮 | 主行租户名 + 副行用户名 | 主行当前租户名 + 副行 `admin` |
| 租户列表（可切） | 无 | 有 |
| 新建租户 | 无 | 有 |
| 账号管理 | 无 | 有（跳「账号」页） |
| 设置 | 有 | 有 |
| 登出 | 有 | 有 |

租户名放主行、用户名放副行：租户是**数据作用域**，弄错了不会报错、只会安静地把数据写到别处；身份弄错则会立刻撞上权限错误。这与当初把租户名常驻在按钮上的理由一致。

**新增「账号」页**（`AccountsPage.tsx`，路由 `/admin/accounts`）：列表 + 新建 + 禁用/启用 + 重置密码。**不进侧边栏 `NAV_GROUPS`**——它对 member 不存在，放进侧边栏会让两种角色看到不同的侧边栏，破坏"侧边栏是固定的"这一心智模型。入口只在账号菜单里，与「设置」一致。

member 直接访问 `/admin/accounts` 时显示无权限提示，而非 404——404 会让人以为是链接坏了而反复重试。

**设置页**新增「修改密码」区块（旧密码 + 新密码 + 确认），两种角色都有。

**前端渲染不承担安全责任。** 按 role 渲染只是为了不误导人；真正的门是 4.5 与 `require_admin_role`。

### 4.8 迁移与播种

启动时（`app/main.py` 的 lifespan 内）：

1. `ensure_admin_users_schema(review_conn)` —— 建表，幂等
2. 查 `admin_users` 中有无 `role='admin'` 的行：
   - **有** → 什么都不做。此时 `settings.admin_token` 是否为空**无关紧要**，它已经完成使命
   - **无** → 用 `settings.admin_token` 作为初始密码播种 `username='admin'`；若该值为空，**抛异常，进程启动失败**

启动失败这一处理方式与工具注册表一致（`app/main.py` 现有注释：「需要立刻发现并修复的部署错误，不是暂时不可用、稍后自动恢复的瞬时故障」）。启动成功但无人能登录是最坏的形态——运维会以为是自己记错了密码，而不是去看配置。

播种在 admin 行**已存在时不执行**，因此改密后重启不会被环境变量覆盖。admin 被误删时重启会按环境变量重建，这是一条免费的恢复路径。

**测试残留租户禁用**：一次性迁移，把以下 6 个租户置为 `disabled`：

`t_verify`、`t_verify2`、`review-test`、`review-ontology-test`、`e2e_concurrency_test`、`table_extract_test`

判定依据：这 6 个在 `terms` / `ingested_documents` / `graph_review_queue` / `etl_runs` 中**均无任何记录**，仅在 `tenant_relation_types` 留有痕迹（它们是当初 `ensure_tenants_schema` 从历史表回填时被发现的）。禁用可逆（`/enable` 接口已存在），不删除任何数据。

保留 `active` 的租户：`demo`（20017 实体 / 1 文档 / 3 待审 / 2 次 ETL）、`default`（2 实体 / 4 类型）。

**不自动创建普通账号。** 自动创建就得自动生成初始密码，而那个密码没有交付渠道——写进日志是泄露，打印在启动输出里没人看得到，只存进数据库则谁也不知道它是什么。等于创建了一批无人能用的账号。账号由 `admin` 登录后按需创建，密码由创建者当面交付。

## 5. 测试策略

### 后端

| 层 | 内容 |
| --- | --- |
| 密码哈希 | 同一密码两次哈希得到不同结果（salt 随机）；校验通过；错误密码不通过；篡改存储串的任一段都不通过；参数从存储串读取（构造一个非默认参数的串仍能校验） |
| 登录 | 正确凭证返回 session + role + tenant_id；错误密码、不存在的用户、已禁用账号返回**同一条**文案与状态码；连续 5 次失败后第 6 次返回 429；成功登录清零计数；`last_login_at` 被更新 |
| 租户隔离 | member 访问自己租户 200；访问他人租户 **403**；admin 访问任意租户 200；禁用后原 session 立即失效 |
| 结构测试 | 见 4.5 防线二，两个方向都要断言 |
| 账号管理 | 四条不变量各一条测试；非 admin 调用 accounts 接口返回 403；响应中不含 `password_hash` |
| 播种 | 空表时播种；已有 admin 时不覆盖；`admin_token` 为空时启动抛异常 |

### 前端

| 内容 |
| --- |
| 登录页提交用户名+密码，成功后跳转 |
| member 的账号菜单**没有**租户列表、新建租户、账号管理三项；有设置与登出 |
| admin 的账号菜单五项齐全 |
| member 的 `setTenantId` 无效——切换尝试后 `tenantId` 不变 |
| member 直达 `/admin/accounts` 看到无权限提示，不是 404 |
| 左下角同时显示租户名与用户名 |

**每条否定式断言（「X 不应该出现」）写完后，必须故意破坏实现确认它会变红。** 断言写在正确和错误实现都能通过的位置，是本项目反复出现的问题。

## 6. 范围外

- **前台问答界面的登录。** 前台 `/` 维持"打开即用"，`tenant_id` 仍由客户端提供。
- **前台的租户伪造问题。** 这是真实存在的洞（`CUSTOMER_RAG_GATEWAY_SHARED_SECRET` 留空时任何调用方可伪造租户身份，且无任何报错），但正确的修法是配置网关共享密钥，见 `docs/superpowers/specs/2026-08-06-gateway-tenant-auth-design.md`，不是给前台套一个后台账号体系。
- **session 持久化。** 重启掉线的现状不变。
- **多 admin。** 见 4.6 不变量 4。
- **审计日志。** 「谁在什么时候做了什么」目前只有 `last_login_at`。真正的操作审计是独立议题。
- **密码找回。** 忘记密码由 admin 重置；admin 自己忘记则清空 `admin_users` 表后重启，按环境变量重新播种。

## 7. 未决风险

1. **403 会被误判成"改坏了"。** 加上租户校验后，前端任何使用了错误 `tenant_id` 的地方会从"安静返回别人的数据"变成 403。那不是新缺陷，是原本就存在、只是从未可见的缺陷浮出水面。阶段二上线后若出现 403，第一反应应是查前端传了什么，而不是回滚校验。

2. **环境变量与实际密码不一致。** 决定 4 的固有副作用。缓解手段是文档，而文档会被跳过——若 admin 改密后遗忘，恢复路径是清空 `admin_users` 表后重启。这条恢复路径必须写进 README。

3. **测试残留租户的判定由分析得出，非用户确认。** 证据充分（零业务数据 + 名称含 test/verify），且禁用可逆、不删数据，故风险可接受。若其中某个实际有用，`/enable` 即可恢复。

4. **限流不覆盖不存在的用户名。** 见 4.3 已知局限。
