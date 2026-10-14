# EPICS Field Client

本地运行的 EPICS Channel Access（CA）连接检测工具。页面直接显示系统、软件依赖、已有 CA 配置、网卡、路由、PV 读取结果，并支持导出 Markdown／JSON 报告。无需模型或远程应用服务器。

## Linux 启动

需要 Python 3.11+、`venv/ensurepip` 和 `iproute2`。`caget` 可选，存在时用于对照读取。系统组件由管理员准备，程序不会自行安装系统软件。

```sh
sh setup.sh
sh start.sh
```

打开启动时打印的本机地址，默认是 `http://127.0.0.1:8765/`。服务只监听回环地址。

## 检测设备

1. 页面自动收集运行终端的实际信息并直接展示。
2. 填写 IOC／CA 网关地址和一个已知可读的 PV。已有环境地址会自动填入；格式一致时沿用环境，改动后使用显式配置。
3. 点击「测试设备连接」，查看实际服务地址、读数、访问权限和各环节结果。
4. 点击「导出报告」或「导出 JSON 数据」。输出保存在本机，不会自动发送。

「检查软件读取功能」使用本机回环地址上的模拟 IOC，不构成真实设备验证。读取成功也不自动代表设备健康或与基准一致。

## 运行与安装选项

- `--no-browser`：不自动打开浏览器，适合无图形界面的 Linux 主机。
- `--port`：更改本机页面端口。
- `--data-dir`：指定本机配置与报告目录，建议放在源码目录之外。

远程 Linux 主机可通过经过批准的 SSH 本地端口转发访问页面。不要将 IOC 或本工具直接暴露到公网。

内网不能安装依赖时，可在系统、架构及 Python 版本匹配的联网机器准备：

```sh
python -m pip download -r requirements.txt -d wheelhouse
```

将源码与 `wheelhouse/` 一起转入；安装脚本发现该目录后只做离线安装。虚拟环境不要跨平台复制。Windows 开发预览可使用 `setup.cmd`／`start.cmd`。

## 范围与隐私

本版本仅支持 CA，不支持 PVA-only 数据或 `EPICS_CA_NAME_SERVERS`。连接配置沿用白名单 CA 环境变量或显式地址，不修改系统环境。

客户端只调用固定的查询工具，LabMCP 启动时设置只读及禁止写入的 allow-list。未提供任意命令或任意 MCP 工具执行接口。它不代替 IOC／网关访问控制和设备联锁。

报告可能包含运行者的主机名、IP、路径和设备读数，应保留在受控环境并在分享前检查。源码及发布包不包含真实报告、截图、凭据或环境快照。程序不读取个人文档或浏览器数据；报告只采集指定的 CA 环境变量，设备子进程仍继承启动程序的进程环境。网页只能导出本次运行生成的报告。

FastMCP 自动更新查询及遥测已关闭；页面资源均为本地文件。初次依赖安装仍可能访问软件包仓库。

## 测试

```sh
FIELD_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -v
```

集成测试只连接回环参考 IOC；另有本机环境采集、只读边界和历史报告访问隔离测试。

## 发布

参见 [PUBLICATION.md](PUBLICATION.md)。导出与诊断数据是本地运行产物，不属于公开源码。

依赖为公开的 [LabMCP EPICS](https://github.com/K-Dense-AI/lab-instrument-mcps/tree/main/servers/protocols/epics)、MCP Python SDK 和 caproto；未复制其源码，依赖各自的许可证继续适用。
