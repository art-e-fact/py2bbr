# py2bbr

[en](README.md) | ja

Python to Bitbake Recipe。

Python パッケージを Yocto Project の *Bitbake レシピ* に変換します。通常は
Yocto レイヤー内で使うため、PetaLinux プロジェクトも含みます。

### 概要とステータス

`py2bbr` は PyPI 互換の任意のインデックスからパッケージを取得し(`--extra-index-url`
などの pip フラグを尊重するため、プライベートインデックスでも動作します)、そこから
Bitbake 変数を導出して `python3-<name>.bb` レシピを生成します---任意で完全な Yocto
レイヤーにまとめることもできます。また、Python パッケージや OE レイヤー/レシピを
含む依存ツリー全体の処理も試みます。デフォルトでは Yocto 5.0 シリーズ (Scarthgap) を
対象とし、ローカルのツールチェーンが許す範囲で生成されたレシピを検証します。

`kas` のサポートは実験的ですが、これまでのところテストはごくわずかです。

### 背景

レシピを手書きするのは難しく、繰り返しが多く、ライセンスファイルを探し回り、
`LIC_FILES_CHKSUM` を計算し、`Requires-Dist` を `RDEPENDS` にマッピングし、適切な
`inherit` クラスを選ぶ必要があります。`py2bbr` は、対象パッケージとその形式的な
依存関係から直接、そうした作業を機械的に行う試みです。


## インストール

いずれは PyPi から提供したいと考えています。現時点ではリポジトリから、または
ツールラッパー経由でインストールしてください。例:

```
uv tool install git+https://github.com/art-e-fact/py2bbr

# または

pipx install git+https://github.com/art-e-fact/py2bbr
```

## 開発用インストール

Python **3.11+** と [uv](https://docs.astral.sh/uv/) で開発しています。

```console
uv sync --dev
uv run py2bbr --help

# テスト
uv run pytest
```

テストに関する注意: 一部のテストは環境で GNU `sed` が利用可能である必要があります。
BSD 系システムではこれらのテストはスキップされます。GNU 版を `sed` または `gsed`
という名前でインストールすることで実行できます。


## 使い方

```console
# `pyserial` 用のレシピ + レイヤー(+ 実験的な kas ビルドファイル)を生成(デフォルトは Scarthgap)
py2bbr pyserial

# バージョン固定(トップパッケージのみ)、Yocto の選択
py2bbr "requests==2.34.2" --yocto-version 5.0

# 対象ランタイムやアーキテクチャを指定(デフォルト: このインタプリタ、このマシン)
py2bbr pyvizionsdk --python-version 3.12 --target-arch aarch64

# 代替またはプライベートインデックス
py2bbr mytool \
    --extra-index-url https://pypi.mycompany.com/+simple \
    --trusted-host pypi.mycompany.com
```

注: デフォルトでは `py2bbr` は次の 2 つのルールに従って依存関係をバンドルします:

1. バージョン要件に厳密に従う(`numpy==1.2.3` または `numpy>=1.2.3`)
2. バージョンの上限がなければ、自動的にメジャーバージョンを予約する(`numpy>=1.2.3`
   は `numpy>=1.2.3,<2` になる)

2 つ目のルールは `--no-major-bump-cap` で無効化できます。

### 出力

通常は [Yocto Poky](https://github.com/yoctoproject/poky) の `build` フォルダの下に
出力されます:

```
.
├── meta-pyserial/
│   ├── README
│   ├── conf/layer.conf
│   └── recipes-devtools/python/python3-pyserial_3.5.bb
└── py2bbr.kas.yml
```

レイヤーはパッケージ名にちなんで命名されるため(`meta-<package>`、例えば
`async_pyserial` なら `meta-async-pyserial`)、生成されるレイヤーはすべて一意になり
ます。`--layer-name` で上書きできます。


## 主なオプション

| フラグ | 意味 |
| --- | --- |
| `--build-class` | 継承するビルドクラスを上書きする(デフォルトの自動は `setuptools3`)。 |
| `--check {structural,parse,checklayer,build}` | 検証レベル(デフォルトは `parse`)。 |
| `--index-url`, `--extra-index-url`, `--trusted-host` | `pip download` に転送される pip インデックスオプションのサブセット。 |
| `--jobs N` | 依存関係を並列で取得する(デフォルトは `4`、`1` は逐次)。 |
| `--keep-sources` | デバッグ用にダウンロードしたファイルを保持する。 |
| `--layer-name` | レイヤーディレクトリ名を上書きする(デフォルト: `meta-<package>`、例: `meta-async-pyserial`)。 |
| `--max-depth N` | 依存ツリーをたどる深さの上限(デフォルト: 無制限)。 |
| `--no-deps` | 依存関係の解決を無効化する(デフォルトは有効): トップのレシピのみを生成し、その RDEPENDS は他のレイヤー(例: meta-python)に委ねる。 |
| `--no-layer`, `--no-kas` | レシピのみを生成し、kas ファイルをスキップする。 |
| `--no-major-bump-cap` | 依存関係のメジャーバージョンを上限付けしない。デフォルトでは、下限のみを持つすべての依存関係(`numpy>=1.21.0`)は `numpy>=1.21.0,<2` として解決される---メジャー系列(1.x)の最新リリースであり、後のメジャー(2.x)はビルドを壊す可能性があるため使わない(典型的には numpy 2.x が OpenCV を壊すのを避けるため)。強い境界(`~=`, `<`, `<=`, `==`)はそのまま扱われる。 |
| `--pip-arg` | `pip download` に任意の生の引数を追加する(繰り返し指定可能)。 |
| `--python-version` | 対象イメージが実行する Python バージョン(`3.12`)。デフォルト: py2bbr 自身が動作するインタプリタ。 |
| `-q`/`--quiet`, `-v`/`--verbose` | ログ出力: stderr に INFO ステージ進行行(デフォルト)、`-q` は警告/エラーのみに制限(ライブ取得の進捗バーも無効化)、`-v` は DEBUG の詳細(パッケージごとの取得/解析サマリー、公開 PyPI への到達性)を追加する。 |
| `--skip-dep NAME` | クロージャから依存関係を除外する(繰り返し指定可能、正規名)。 |
| `--target-arch`, `--arch` | 対象イメージが実行する CPU を `platform.machine()` が報告する形式で指定(`aarch64`, `armv7l`, `x86_64`, `riscv64`)。デフォルト: このマシンのアーキテクチャ。 |
| `--targets LIST` | 最終レポートをこれらのターゲットキーに限定する(カンマ区切り、例: `zynqmp,versal`)。不明なキーは報告され無視される。デフォルト: リスト全体。 |
| `--with-deps` | 依存ツリーをたどり、同じレイヤー内の各メンバーにつき 1 つのレシピをレンダリングする(デフォルト、bitbake は RDEPENDS をたどる)。 |
| `--yocto-version` | Yocto のバージョンまたはコードネーム(`5.0`、`scarthgap` など)。デフォルトは `5.0`。 |


## 生成の流れ

`py2bbr` が何を行うかの概要は次のとおりです:

1. 対象パッケージを `pip download` する(sdist 優先、wheel をフォールバック)。
   wheel にフォールバックする場合は、対象ランタイムとアーキテクチャに固定される。
2. `PKG-INFO`/`METADATA` を解析し、ビルドシステムと依存関係について
   `pyproject.toml` を読み、ライセンスファイルとダイジェストを確認する。
3. [`packaging`](https://pypi.org/project/packaging/) で `Requires-Dist` を分類する。
4. ソースにコンパイル済みコード(`.c`/`.cpp`/`.pyx`/`.rs`/...、`setup.py` の
   `ext_modules`、ネイティブビルド要件)がないかなどを調べる。
5. レシピ(`inherit pypi setuptools3` パターン)と、パッケージ名にちなんで命名した
   `meta-<package>` レイヤー、そして `kas` ビルドファイルをレンダリングする。
6. 検証レベルを実行する。
7. レポートする。


## 対象ランタイム

`py2bbr` はデフォルトで、実行中のランタイムとアーキテクチャ用にレイヤーとレシピを
生成します。それぞれの任意引数を使って、異なる Python ランタイムやアーキテクチャ用
に生成することもできます:

```bash
# 64 ビット ARM CPU で Python 3.12 が動作する古めの PetaLinux イメージ
py2bbr pyvizionsdk --python-version 3.12 --target-arch aarch64
```

| | デフォルト | 上書き方法 |
| --- | --- | --- |
| Python バージョン | `py2bbr` 自身が動作するインタプリタ | `--python-version 3.12`(`3.12.4` と `312` も現在は受け付けるが `3.12` に縮約される) |
| CPU アーキテクチャ | このマシン(`platform.machine()`) | `--target-arch aarch64`(`--arch`; `arm64` --> `aarch64`、`amd64` --> `x86_64`、`armv7` --> `armv7l`) |

解決された値は表示され(`Runtime : Python 3.12 on aarch64`)、レシピに記録されます。


## チェック

チェックは、生成されたレイヤーとレシピを可能な限り検証しようとします。チェックは
価値とコストの異なるレベルに整理され、必要なツールも異なります(一部は `bitbake`
が必要など)。`--check LEVEL` で選択し、`LEVEL` までのすべてのチェックが実行され
ます。現在の環境で実行できないステージは `SKIPPED` として報告されます。

| レベル | 内容 | 必要なもの |
| --- | --- | --- |
| `structural` | `.bb` に対する純粋な Python チェック(必須変数、引用符/括弧の対応、破損の痕跡: タイムスタンプの残り、`-` で始まる依存トークン、単一引用符の対応)。 | なし |
| `parse` | `bitbake -p <pn>`(パースのみ)。 | `bitbake` + source 済みの `BUILDDIR` |
| `checklayer` | `yocto-check-layer <layer>`(高コストな `bitbake world` を実行)。 | `yocto-check-layer` + `BUILDDIR` |
| `build` | `kas build -c <kasfile>`(フルビルド)。 | `kas` + ネットワーク |


## 対応する Yocto シリーズ

`py2bbr` は Yocto のバージョン/コードネーム / Poky ブランチ / 対象 Python のマッピング
を持っています。現在は **6.0 (Wayne)** と **5.0 (Scarthgap、デフォルト)** を
サポートしています。4.3 (Styhead)、4.1 (Mickledore)、3.1 (Dunfell) も動作すると
予想していますが未テストです(そのため現時点ではコード内でコメントアウトされて
います)。さらにサポートを増やすには `src/py2bbr/yocto.py` にエントリを追加して
ください。

エンドツーエンドテストは現在 5.0(デフォルト)で実行されており、その他は現時点では
LLM が生成したコードを使った「実験的」なものです。


## 例

### PetaLinux プロジェクトに async-pyserial を追加する

py2bbr の具体的なユースケースは、PetaLinux などで管理されるプロジェクトに追加する
レイヤー/レシピを作成することです。以下は、PyPi で入手できる `async-pyserial`
パッケージを PetaLinux 2024.2 プロジェクトに追加する例です(私たちの現在の
プロジェクトの一つに合わせて選んだもので、特別な点はなく、他のバージョンでも
ドキュメントを再確認すれば動作するはずです)。

前提:

* `pl-project` フォルダの下に作成された PetaLinux 2024.2 プロジェクト。PetaLinux
  ディストリビューションは、デフォルトで Python パッケージを受け入れるのに十分
  新しいか、受け入れるように設定されていること(通常は `petalinux-config` コマンド)。
* py2bbr で作成した `async-pyserial` 用のレイヤー/レシピ:
  `uv run py2bbr async-pyserial` を `mylayer` フォルダの下で実行。現在は
  `mylayer/build/meta-async-pyserial` フォルダなどを作成します。
* `py2bbr` は PetaLinux と同じ Yocto バージョンを対象に呼び出されている。ここでは
  両方とも Yocto 5.0 シリーズ、Scarthgap を使用。

手順は次のとおりです:

1. レイヤーをプロジェクトにコピーする(ドキュメント上の場所、UG1144「Adding Layers」):

```
cp -r mylayer/build/meta-async-pyserial pl-project/project-spec/
```

2. レイヤーを登録する

```
cd pl-project
petalinux-config   # メニュー: Yocto Settings --> User Layers
# 入力: ${PROOT}/project-spec/meta-async-pyserial
# 保存して終了
レイヤーが <proj>/build/conf/bblayers.conf に現れることを確認する。
```

3. rootfs にパッケージを追加する:

```
echo 'CONFIG_python3-async-pyserial' >> pl-project/project-spec/meta-user/conf/user-rootfsconfig
petalinux-config -c rootfs   # メニュー: user packages --> enable python3-async-pyserial
```

4. 再ビルドする:

```
petalinux-build
```

5. ターゲット上で確認する(例: QEMU 内、またはデバイス上で直接):

```
python3 -c "import async_pyserial"
```

## 開示

この作業には、以下を使用した手書きおよび生成されたコードが含まれています:

* ローカル版 Qwen 3.8
* オンライン版 OpenCode Big Pickle


## ライセンス

MIT — [LICENSE](LICENSE) を参照。
