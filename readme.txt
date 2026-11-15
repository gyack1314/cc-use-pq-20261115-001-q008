秋窗客流分析账本
运行环境：Python 3.11，已按 requirements.txt 预装依赖。
启动：python -m autumn_ledger serve --database autumn-ledger.sqlite3
接口说明：服务启动后访问 /docs。
合成业务演练：python -B -m autumn_ledger demo
测试：python -B -m unittest discover -s tests -v
构建：python -B -m autumn_ledger.check_source
数据完整性：python -B -m autumn_ledger check --database autumn-ledger.sqlite3
金额单位为人民币分；订单修订为全量快照，退款为累计值。
出游日期是供应商给出的目的地本地日期；事件时间必须含时区。
取消不计出游人数，未退收入仍计入净额。缺报不等于零需求。
本地演练使用虚构数据，分析仅描述所选来源样本。
数据使用撤回：POST /sources/{source}/withdrawals 按订单或批次撤回授权（幂等），GET /withdrawals/{id}/impact 查看受影响的分析、频道与归档。
撤回保留订单事件用于内部对账；含撤回贡献的版本立即停止对外提供，替代分析通过既有审核后频道才恢复；已下载到外部的文件无法收回。
