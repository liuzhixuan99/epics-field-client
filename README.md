# EPICS Field Client

本地运行的 EPICS Channel Access（CA）连接检测工具。页面直接显示系统、软件依赖、已有 CA 配置、网卡、路由、PV 读取结果，并支持导出 Markdown／JSON 报告。无需模型或远程应用服务器。

v0.1.1 支持只填写已知 PV、沿用本机配置／默认发现，以及单独选择同网段广播或手动搜索地址。Windows／Linux 独立运行包包含相同更新。

在 [GitHub Releases](https://github.com/liuzhixuan99/epics-field-client/releases/latest) 下载对应系统的运行包。Windows 选择 `epics-field-client-windows-x64.zip`；Linux 选择 `epics-field-client-linux-x64.tar.gz`（x86-64、glibc 2.39+，构建环境 Ubuntu 24.04）。解压整个文件夹后启动应用。

## 双击启动（推荐）

使用对应系统的独立运行包，无需安装 Python、依赖或 WSL。

- **Windows**：解压 Windows 包，双击 `EPICS-Field-Client.exe`。
- **Linux 桌面**：解压 Linux 包，双击 `EPICS-Field-Client`；应用会打开系统终端。需要可用的桌面终端和浏览器。若文件管理器询问执行方式，选择「运行」。

两种系统都会自动打开本机浏览器页面。**运行终端是服务的开关：关闭该终端或按 Ctrl+C，服务及它启动的 MCP／软件自检子进程随之停止。** 关闭浏览器标签页不会停止服务。页面只监听本机回环地址；默认端口占用时自动选择空闲端口。

配置和报告默认保存在 Windows 的 `%LOCALAPPDATA%/EPICS Field Client` 或 Linux 的 `~/.local/share/epics-field-client`（遵循 `XDG_DATA_HOME`），不写入应用包。自动启动不会启动现场 IOC；「检查软件读取功能」才会临时运行本机仿真。

Linux 包按构建机架构与 glibc 版本生成，不能跨 CPU 架构使用。Windows 包用于 Windows x64，直接在 Windows 运行，无需 Linux。

## 从源码启动（开发者）

Windows 可双击 `start.cmd`；已有 Python 3.11+ 时，第一次自动准备 `.windows-venv`。Linux 的 `start.sh` 在首次运行时自动准备 `.venv`。两种运行环境分别保存，不能互换。

需要 Python 3.11+、`venv/ensurepip` 和 `iproute2`。`caget` 可选，存在时用于对照读取。系统组件由管理员准备，程序不会自行安装系统软件。

```sh
sh setup.sh
sh start.sh
```

打开启动时打印的本机地址，默认是 `http://127.0.0.1:8765/`。服务只监听回环地址。

## 检测设备

1. 页面自动收集运行终端的实际信息并直接展示。
2. 填写一个已知可读的 PV，默认选择「沿用本机 EPICS 配置／默认发现」。无需先知道 IOC 地址：未设置 CA 环境变量时，CA 默认向本机网卡所在子网广播查询指定 PV。这里的本机配置指启动本工具的进程环境，其他终端或控制软件内的配置不会自动导入。
3. 点击「测试设备连接」，查看发现的实际 PV 服务地址、读数、访问权限和各环节结果。该服务可能为 IOC 或 CA 网关；搜索地址与最终服务地址不一定相同。
4. 点击「导出报告」或「导出 JSON 数据」。输出保存在本机，不会自动发送。

「检查软件读取功能」使用本机回环地址上的模拟 IOC，不构成真实设备验证。读取成功也不自动代表设备健康或与基准一致。

连接方式可选：

- **沿用本机配置／默认发现**：保留本机允许的 CA 环境变量；未设置的参数由驱动使用默认值。如果已有配置关闭自动广播，程序会尊重该设置。
- **同网段自动发现指定 PV**：不使用已有地址列表，明确开启本机子网广播。广播通常不能跨网段，不会枚举全部 IOC 或 PV。
- **手动指定 CA 搜索地址**：填写 IOC、CA 网关或管理员提供的子网广播地址，可带 `:端口`，多个地址用空格分隔；可选择同时广播。端口沿用本机环境，未设置时使用默认搜索端口 UDP 5064。CA 是协议，没有统一的默认 IOC IP。

自动发现失败时，先确认 PV 名称与原控制软件一致，再检查跨网段、VPN、网卡、防火墙及读取权限；需要时向管理员获取搜索地址。失败不等于现场没有 IOC。检测只在点击测试或加载工具时查询指定 PV，页面打开仅采集本机信息。

## 手动运行 LabMCP 工具

设备连接通过后，页面自动加载当前 LabMCP 服务返回的全部工具，也可点击「加载／刷新工具」。选择工具后，页面根据真实参数格式生成字段；列表、对象和多种类型的值用 JSON 填写。运行结果直接显示，可单独导出 JSON。

默认只读服务返回 8 个工具。可手动读取单个／多个 PV、查看权限、持续监测（单次最多 60 秒）、查看操作日志和重连。工具连接在多次调用之间保留；修改地址或 PV 后需重新加载。未配置现场安全动作的 `apply_safe_state` 显示为不可执行。

如确需写入，经现场负责人确认后，用启动参数明确列出允许写入的精确 PV 名称：

```sh
sh start.sh --write-pv TEST:SETPOINT --write-pv TEST:ENABLE
```

此时手动工具服务返回 10 个工具，包括 `put_pv` 和 `put_pvs`。页面需勾选本次写入确认，服务端与 LabMCP 均校验 PV 允许清单；不会开放任意 PV 写入或安全动作。批量写入最多 10 个 PV，每项等待最多 60 秒。写入异常或超时不代表设备未执行，必须先读回核实，不自动重试。自动设备连接检测仍只执行读取。

## 运行与安装选项

- `--no-browser`：不自动打开浏览器，适合无图形界面的 Linux 主机。
- `--port`：更改本机页面端口。
- `--data-dir`：指定本机配置与报告目录，建议放在源码目录之外。
- `--write-pv`：允许手动写入的精确 PV 名称，可重复；省略时只读。

远程 Linux 主机可通过经过批准的 SSH 本地端口转发访问页面。不要将 IOC 或本工具直接暴露到公网。

内网不能安装依赖时，可在系统、架构及 Python 版本匹配的联网机器准备：

```sh
python -m pip download -r requirements.txt -d wheelhouse
```

将源码与 `wheelhouse/` 一起转入；安装脚本发现该目录后只做离线安装。虚拟环境不要跨平台复制。Windows 源码运行可使用 `setup.cmd`／`start.cmd`。

## 范围与隐私

本版本仅支持 CA，不支持 PVA-only 数据或 `EPICS_CA_NAME_SERVERS`。连接配置沿用白名单 CA 环境变量或显式地址，不修改系统环境。

默认只读：自动检测调用固定查询工具，手动调用限于已核实的 LabMCP EPICS 工具并校验参数格式。写入仅在启动时指定 PV 后开放；即使启用写入，安全动作工具仍不可执行。未提供任意命令或自定义 MCP 服务地址／程序执行接口。它不代替 IOC／网关访问控制和设备联锁。

报告可能包含运行者的主机名、IP、路径和设备读数，应保留在受控环境并在分享前检查。源码及发布包不包含真实报告、截图、凭据或环境快照。程序不读取个人文档或浏览器数据；报告只采集指定的 CA 环境变量，设备子进程仍继承启动程序的进程环境。网页只能导出本次运行生成的报告。

FastMCP 自动更新查询及遥测已关闭；页面资源均为本地文件。初次依赖安装仍可能访问软件包仓库。

## 测试

页面连接方式回归检查（需 Node.js）：`node tests/test_web.js`。

```sh
FIELD_INTEGRATION=1 .venv/bin/python -m unittest discover -s tests -v
```

集成测试只连接回环参考 IOC；另有本机环境采集、只读边界和历史报告访问隔离测试。

## 发布

参见 [PUBLICATION.md](PUBLICATION.md)。导出与诊断数据是本地运行产物，不属于公开源码。

独立运行包在对应系统安装 PyInstaller 6.16 后，用 `python -m PyInstaller --noconfirm desktop.spec` 构建。交付整个 `dist/EPICS-Field-Client/` 文件夹，不能仅复制其中的可执行文件。独立包包含公开依赖的运行库，其许可证继续适用。构建产物也须单独做发布检查。

依赖为公开的 [LabMCP EPICS](https://github.com/K-Dense-AI/lab-instrument-mcps/tree/main/servers/protocols/epics)、MCP Python SDK 和 caproto。独立包包含运行所需的公开依赖和 Python 运行库，其各自的许可证继续适用；声明见 `THIRD_PARTY_NOTICES.txt` 和包内的许可证文件。
