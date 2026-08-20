# 走查记录

`curriculum.json` 的每个 `verified` 单元都登记至少一份非作者走查记录；manifest 是单元状态与记录路径的
机器权威。本目录保存这些增量记录以及 release audit，不另维护手工状态清单。

[1.0.0 非作者发布审计](release-1.0.0.md) 在 clean candidate checkout 上复核了 76/76 source
ownership、generated-output provenance、问题迁移、完整 publication pipeline、build/QEMU/GDB
regressions 与 cleanup，结果为 PASS。
