---
name: ssh
description: SSH client tools — run a command on a remote host, transfer files over SFTP, and probe reachability. Credentials (private key, password, sudo password) live in Settings (skill_configs.ssh); they are never passed as tool arguments. Use when the user asks to log into, run something on, or copy files to/from a remote machine.
metadata:
  kind: skill
  loader: manifest.yaml
  tools: ssh_exec, ssh_upload, ssh_download, ssh_probe
---

# SSH（ssh）

远端命令与文件传输。凭据只在设置页配，模型看不到值。

## 先配置

设置页 → 技能 → SSH：

- 私钥路径（推荐），可选口令
- 或者密码（只有密码的新机器，例如刚烧好的树莓派）
- 可选 sudo 密码（远端 sudo 不是免密时）

也可设 `CRANNIA_SSH_KEY` 环境变量给私钥路径。没配时工具照常注册，
调用回一句"没配 SSH 凭据"并给出路线 —— 不是功能不存在。

## 三条审批（第一次都会碰到）

1. 出网审批：目标单位是 `user@host`。四选项：同意本次 / 本次对话不再问 /
   信任此目标（永久，写入白名单，设置页可撤销）/ 不同意。
2. 首次连接的主机密钥：未知主机默认拒绝。取指纹、给你核对，信任后才写入
   框架自己的 known_hosts（不碰你的 ~/.ssh/known_hosts）。
3. 上传/下载的本地路径：上传的源文件过框架的路径分级（.env 一族读不到）；
   下载的落点必须在工作区。

## 什么时候用

- 远端执行命令/查状态 ⇒ `ssh_exec`
- 传文件上去 / 取下来 ⇒ `ssh_upload` / `ssh_download`（SFTP）
- 只想知道端口通不通 ⇒ `ssh_probe`（不需要凭据）
- 要把远端某个端口映射到本地（隧道 / 端口转发）⇒ `ssh_forward`
  （立刻返回 task_id，隧道在后台跑，30 分钟寿限；`task` 工具可查可停）

## 隧道（`ssh_forward`）

`ssh_forward(host, remote_host, remote_port, local_port=0)` 在 `127.0.0.1`
上开一个监听，流量经 SSH 转发到 `remote_host:remote_port`。
`local_port=0` 表示由系统挑一个空闲端口（回执里给你实际端口）。
它是 nc / socat 那类 relay 的正规替身；寿限 30 分钟，要续就重新调用。

## 长任务

`ssh_exec` 有超时（默认 20 秒）。`apt upgrade`、编译这类：

```bash
nohup <命令> > /tmp/x.log 2>&1 &
```

然后 `ssh_exec("tail -n 50 /tmp/x.log")` 轮询。框架的退出码与会话无关。

## 改网络配置前先想回滚

改 Wi-Fi / IP / 防火墙可能把连接本身切断。动手前：

- 确认还有第二条进得去的路（串口、本地屏幕、另一张网卡）
- 先验证新配置不依赖当前会话
- 大改之前留一份原配置（`cp /etc/... /etc/...bak`）

工具不会机械化拦这一步，这是使用纪律。

## 什么时候不用

- 本机命令 ⇒ `run_command`
- 要操作远程机器的图形界面 ⇒ 在本机开 RDP/VM 客户端窗口，用 `computer` 接管那个窗口
- 批量多机配置 ⇒ 先逐台 `ssh_exec`，或让用户上 Ansible

## 安全边界

- 凭据不进工具参数、不回显；exec 输出有上限、有超时
- 主机密钥严格（RejectPolicy）：密钥与记录不一致一律拒，不给"警告并继续"
- 每次调用写一行 EGRESS 审计（目标 / 动作 / 结果）
- 连接走 paramiko（LGPL-2.1），不依赖系统 ssh 命令
- `.local` 主机名在 Windows 上常常解析不了 —— 用 IP，或安装 Bonjour
