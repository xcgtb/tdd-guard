# TTD Guard 1.8.5 修复包

覆盖到现有 1.8.4 源码同目录即可。

本包包含：
- 晨报缓存/现场扫逻辑修复
- 晨报 24 小时入库引用后台 5 分钟缓存
- 晨报订阅更新引用实际成功推送汇报
- 晨报 Emby 缺集引用预扫后的最新成功快照
- 晨报 TZ/跨午夜预扫修复
- 双库治理扫描证据索引性能优化
- 治理单执行不再重新全量扫描双库
- 1.8.5 CHANGELOG

验证：279 passed；static_check OK；compileall OK。

Git：本地已创建 commit 201e27e 并打 v1.8.5 tag。当前环境无法访问 github.com，因此 push 需要在可联网环境执行：

    git push origin master
    git push origin v1.8.5
