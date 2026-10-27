"""进销存台账 V1 核心库。

模块:
- decimal_utils  Decimal 精确计算与确定尾差分配
- schema       工作簿表结构与字段字典
- store        存储层(读写/备份/锁/审计/导入登记)
- engine       月末加权成本计算与期间结账
- biz          业务写操作与校验
- importer     CSV/XLSX 导入(预览/确认/幂等)
- queries      查询层
"""
__version__ = "1.0.0"