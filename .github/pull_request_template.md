# Pull Request

## Summary / 摘要

<!-- What changed and why? / 改了什么，为什么改？ -->

## Change Type / 变更类型

- [ ] Documentation / 文档
- [ ] Web cockpit / 仪表盘
- [ ] AI driver or operations / AI 驾驶员或运维
- [ ] Paper trading or execution / 模拟盘或执行
- [ ] Watchdog, Doctor, or scheduler / 监控、自愈或调度
- [ ] Data source or strategy / 数据源或策略
- [ ] Tests or tooling / 测试或工具

## Safety Checklist / 安全清单

- [ ] Keeps `BROKER_MODE=paper` as the documented default.
- [ ] Does not commit `.env`, tokens, account snapshots, runtime databases, logs, or private server details.
- [ ] Does not expose public control endpoints or raw internal status.
- [ ] Does not present LLM output as investment advice or guaranteed returns.
- [ ] Updates README or `docs/` when user-facing behavior changes.

## Verification / 验证

<!-- Paste the commands you ran and their results. / 粘贴你运行过的验证命令和结果。 -->

```text
python3 -m compileall -q web scheduler strategy execution data main.py config.py tests
python3 tests/test_web_public_dashboard.py
```

## Deployment Notes / 部署说明

<!-- Mention rollout, rollback, or Hermes/Linux impact if relevant. / 如涉及部署，请说明上线、回滚或 Hermes/Linux 影响。 -->

