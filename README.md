# megaraid-check

MegaRAID 配下の論理・物理ディスク状態、SMART 属性、MegaRAID Event Log を定期確認し、異常時だけメール通知する Python スクリプトです。

MegaCli の `Media Error Count` などに加え、`smartctl -d megaraid,N` で物理ディスクの SMART を取得し、前回値との差分も監視します。警告には可能な限りモデル名・シリアル番号・WWNを含めるため、交換対象の特定にも利用できます。

## 主な機能

- MegaRAID Adapter の Degraded / Failed Disks 監視
- Virtual Drive の `State` / `Bad Blocks Exist` 監視
- Physical Drive の Firmware state / SMART alert 監視
- Media Error / Other Error / Predictive Failure の増加監視
- SMART の主要属性監視
  - `Reallocated_Sector_Ct`
  - `Reallocated_Event_Count`
  - `Current_Pending_Sector`
  - `Offline_Uncorrectable`
  - `UDMA_CRC_Error_Count`
- `Current_Pending_Sector > 0` と `Offline_Uncorrectable > 0` は継続して警告
- Event Log の新規異常イベントだけを通知
- `lsblk` による論理RAIDデバイスの自動検出
- モデル・シリアル番号・WWNを警告へ表示
- JSON stateによる前回値管理
- 初回導入用 `--initialize`

## 必要環境

- Linux
- Python 3.6 以上
- MegaCli
- smartmontools (`smartctl`)
- util-linux (`lsblk`)
- `mail` コマンド（mailx / s-nail 等）<br>
※環境によってパッケージが異なるためrpmの依存関係でインストールされません。<br>
　手動でインストールを行ってください。

このリポジトリには MegaCli その他の第三者製バイナリは含みません。各ツールは利用者側で入手・インストールしてください。

> **注意:** 本ツールは MegaCli のテキスト出力形式に依存します。コントローラ、ファームウェア、MegaCli のバージョンによっては出力形式が異なり、調整が必要な場合があります。

### MegaCliについて

MegaCliは本リポジトリおよびRPMには含まれていません。

本ツールは設定ファイルの以下のパスにあるMegaCliを使用します。

```ini
[commands]
megacli = /usr/bin/MegaCli
```
環境によってコマンド名や配置先が異なる場合は、megaraid_check.conf を変更してください。

```bash
command -v MegaCli
command -v MegaCli64
```

## RPMインストール（推奨）

(例)

```bash
sudo dnf install ./megaraid-check-1.0.0-1.el9.noarch.rpm
```

## 初期設定

必要に応じて `/etc/megaraid-check/megaraid_check.conf` を修正します。

```bash
sudo vi /etc/megaraid-check/megaraid_check.conf
```

## 初回実行 (--initialize)

既存の累積カウンタをすべて「0から増えた異常」として扱わないよう、初回は `--initialize` を使用することを推奨します。

```bash
sudo /usr/sbin/megaraid-check --initialize
```

現在のRAID/SMART/Event Log状態を `state.json` へ保存し、警告メールは送信しません。

状態を確認しながら初期化する場合:

```bash
sudo /usr/sbin/megaraid-check --initialize --dump
```

## systemd timer有効化

RPMをインストールしただけではtimerは有効になりません。

設定ファイルの確認と `--initialize` の実行後、以下のコマンドで
timerを有効化してください。

```bash
sudo systemctl enable --now megaraid-check.timer
```

### timerの確認

```bash
systemctl status megaraid-check.timer
systemctl list-timers megaraid-check.timer
```

手動でsystemd経由の実行を確認する場合:

```bash
sudo systemctl start megaraid-check.service
systemctl status megaraid-check.service
```

実行ログは以下で確認できます。

```bash
journalctl -u megaraid-check.service
```

### RPMで配置されるファイル

```text
/usr/sbin/megaraid-check
/etc/megaraid-check/megaraid_check.conf
/usr/lib/systemd/system/megaraid-check.service
/usr/lib/systemd/system/megaraid-check.timer
/var/lib/megaraid-check/
└── state.json    # 初回実行時に生成
```

state.json はRPMには含まれず、初回実行時に生成されます。

## 通常実行

```bash
sudo /usr/sbin/megaraid-check
```

異常がなければ何も通知しません。異常が検出された場合は設定した宛先へメールを送信します。

### メールを送らず確認

```bash
sudo /usr/sbin/megaraid-check --no-mail --dump
```

`--no-mail` でもstateは更新されます。単なる閲覧目的で基準値を変えたくない場合は、stateファイルの扱いに注意してください。

### デバッグ

```bash
sudo /usr/sbin/megaraid-check --debug --no-mail
```

### バージョン表示

```bash
/usr/sbin/megaraid-check --version
```

## 終了コード

| Code | 意味 |
|---:|---|
| 0 | 正常、または `--initialize` 完了 |
| 1 | RAID/SMART/Event Log の警告を検出 |
| 2 | 既知の実行エラー（設定、外部コマンド等） |
| 3 | 想定外の例外 |

## stateファイル

既定値:

```text
/var/lib/megaraid-check/state.json
```

WWNを優先キーとして、前回のRAIDカウンタとSMART値、Event Logの最新seqNumを保存します。

一時ファイルへ書き出して `os.replace()` するため、state更新は可能な範囲でatomicに行います。

警告メールの送信に失敗した場合はstateを更新しないため、次回実行時に同じ一過性警告を再度検出できます。

## 監視上の注意

### SMARTの累積値

`Reallocated_Sector_Ct` や `UDMA_CRC_Error_Count` は累積値です。既に値が存在する環境では、初回に `--initialize` を実行してください。

### カウンタのリセット

MegaRAID側の一部カウンタはコントローラやホストの再起動で小さくなる場合があります。現在値が前回値より小さい場合はリセットされたものとして扱い、その後の増加を新規エラーとして検出します。

### 複数MegaRAIDアダプタ

Physical DiskのDevice IDはアダプタ間で重複する可能性があります。本ツールはSMART出力からLU WWNを取得できる場合、MegaCliのWWNと照合して誤った物理ディスクのSMARTを採用しないようにしています。

ただし、コントローラやドライブによってLU WWNがSMART出力に現れない場合があります。複数アダプタ構成では、実環境で `--no-mail --dump` を使って取得結果を必ず確認してください。

## セキュリティ / プライバシー

本ツールはroot権限での実行を前提としています。

警告メールや `--dump` 出力には以下が含まれることがあります。

- ホスト名
- ディスクモデル
- シリアル番号
- WWN
- MegaCli / SMART の詳細出力

公開issueやログ共有の際は、必要に応じてマスキングしてください。実運用用の `megaraid_check.conf` と `state.json` はGitへコミットしないことを推奨します。


## 手動インストール

```bash
sudo install -o root -g root -m 0750 \
  megaraid_check.py \
  /usr/local/sbin/megaraid-check

sudo install -D -o root -g root -m 0600 \
  megaraid_check.conf.example \
  /usr/local/etc/megaraid-check/megaraid_check.conf

sudo mkdir -p /var/lib/megaraid-check
sudo chown root:root /var/lib/megaraid-check
sudo chmod 0700 /var/lib/megaraid-check
```

コマンドの実際のパスは確認してください。

```bash
command -v MegaCli
command -v smartctl
command -v lsblk
command -v mail
```

必要に応じて `/usr/local/etc/megaraid-check/megaraid_check.conf` を修正します。

※手動インストールした場合は、それぞれ

```
/usr/sbin/megaraid-check
/etc/megaraid-check/megaraid_check.conf
```

を

```
/usr/local/sbin/megaraid-check
/usr/local/etc/megaraid-check/megaraid_check.conf
```

に読み替えてください。

## 手動インストール時の実行方法

コマンドを実行する場合は下記のように設定ファイルを指定してください。

```bash
sudo /usr/local/sbin/megaraid-check \
    --config /usr/local/etc/megaraid-check/megaraid_check.conf
```

## cron

`/etc/cron.d/megaraid_check` の例:


```cron
00 01 * * * root /usr/local/sbin/megaraid-check \
  --config /usr/local/etc/megaraid-check/megaraid_check.conf > /dev/null
```

標準出力だけを破棄します。Python例外、設定エラー、MegaCli/smartctl実行失敗などはstderrへ出るため、cron自身のメール通知を残せます。

RAID/SMART異常については本スクリプトがメール送信し、同じ異常をstderrへ再出力しないため、障害メールが二重にならない構成です。

### 注意

cronはsystemd timerと同時に有効化しないよう注意してください。
両方有効だと毎日2回実行されます。

## ライセンス

MIT License。詳細は [LICENSE](LICENSE) を参照してください。

## Development

このプロジェクトは、実機での運用・検証・仕様決定を行いながら、OpenAI ChatGPT の支援を受けて開発されています。
