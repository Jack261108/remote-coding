class TgCliGateway < Formula
  include Language::Python::Virtualenv

  desc "Telegram interactive remote CLI execution gateway"
  homepage "https://github.com/Jack261108/remote-coding"
  url "https://github.com/Jack261108/remote-coding/releases/download/v0.1.2/tg-cli-gateway-0.1.2.tar.gz"
  # sha256 is a placeholder — replaced by release.yml on first publish.
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license "MIT"

  depends_on "python@3.11"

  # === RESOURCES ===
  # URLs and sha256 fetched from PyPI JSON API (https://pypi.org/pypi/<name>/<ver>/json).
  # C-extension packages use sdist; pure-Python packages use py3-none-any wheel.
  # Verify pinned versions satisfy pyproject.toml constraints:
  #   aiogram >=3.13.1,<4 / pydantic-settings >=2.6.1,<3 / aiohttp-socks >=0.10.1,<1

  resource "aiofiles" do
    url "https://files.pythonhosted.org/packages/bc/8a/340a1555ae33d7354dbca4faa54948d76d89a27ceef032c8c3bc661d003e/aiofiles-25.1.0-py3-none-any.whl"
    sha256 "abe311e527c862958650f9438e859c1fa7568a141b22abcd015e120e86a85695"
  end

  resource "aiogram" do
    url "https://files.pythonhosted.org/packages/c9/7b/8bc61691b1fc9d1653e12697436b2742dd1408f14c479d15f46d1ab34be1/aiogram-3.28.2-py3-none-any.whl"
    sha256 "8a90cdf75a64ba629468f73b6e999662943fff39db213bb406afe114a3e8c0f2"
  end

  resource "aiohappyeyeballs" do
    url "https://files.pythonhosted.org/packages/5f/fc/a7bf5b6e4e617b45f90f2d9d2a68519c249c81dd4fc2658c7a2a61c4f4b7/aiohappyeyeballs-2.6.2-py3-none-any.whl"
    sha256 "4708045e2d7a6c6bdf8aafa8ed39649eaf926a4543b54560659129e3365953c4"
  end

  resource "aiohttp" do
    url "https://files.pythonhosted.org/packages/77/9a/152096d4808df8e4268befa55fba462f440f14beab85e8ad9bf990516918/aiohttp-3.13.5.tar.gz"
    sha256 "9d98cc980ecc96be6eb4c1994ce35d28d8b1f5e5208a23b421187d1209dbb7d1"
  end

  resource "aiohttp-socks" do
    url "https://files.pythonhosted.org/packages/bf/7d/4b633d709b8901d59444d2e512b93e72fe62d2b492a040097c3f7ba017bb/aiohttp_socks-0.11.0-py3-none-any.whl"
    sha256 "9aacce57c931b8fbf8f6d333cf3cafe4c35b971b35430309e167a35a8aab9ec1"
  end

  resource "aiosignal" do
    url "https://files.pythonhosted.org/packages/fb/76/641ae371508676492379f16e2fa48f4e2c11741bd63c48be4b12a6b09cba/aiosignal-1.4.0-py3-none-any.whl"
    sha256 "053243f8b92b990551949e63930a839ff0cf0b0ebbe0597b0f3fb19e1a0fe82e"
  end

  resource "annotated-types" do
    url "https://files.pythonhosted.org/packages/78/b6/6307fbef88d9b5ee7421e68d78a9f162e0da4900bc5f5793f6d3d0e34fb8/annotated_types-0.7.0-py3-none-any.whl"
    sha256 "1f02e8b43a8fbbc3f3e0d4f0f4bfc8131bcb4eebe8849b8e5c773f3a1c582a53"
  end

  resource "attrs" do
    url "https://files.pythonhosted.org/packages/64/b4/17d4b0b2a2dc85a6df63d1157e028ed19f90d4cd97c36717afef2bc2f395/attrs-26.1.0-py3-none-any.whl"
    sha256 "c647aa4a12dfbad9333ca4e71fe62ddc36f4e63b2d260a37a8b83d2f043ac309"
  end

  resource "certifi" do
    url "https://files.pythonhosted.org/packages/59/8c/57e832b7af6d7c5abe66eb3fbe3a3a32f4d11ea23a1aa7131371035be991/certifi-2026.5.20-py3-none-any.whl"
    sha256 "3c52e209ba0a4ad7aebe60436a4ab349c39e1e602e8c134221e546902ad25897"
  end

  resource "frozenlist" do
    url "https://files.pythonhosted.org/packages/2d/f5/c831fac6cc817d26fd54c7eaccd04ef7e0288806943f7cc5bbf69f3ac1f0/frozenlist-1.8.0.tar.gz"
    sha256 "3ede829ed8d842f6cd48fc7081d7a41001a56f1f38603f9d49bf3020d59a31ad"
  end

  resource "idna" do
    url "https://files.pythonhosted.org/packages/de/a7/f76514cc40ad6234098ecdebda08732d75964776c51a42845b7da10649e2/idna-3.17-py3-none-any.whl"
    sha256 "466e48829084efe2548012b855df21540b96f2e20e51bd124c851536556a592c"
  end

  resource "magic-filter" do
    url "https://files.pythonhosted.org/packages/cc/75/f620449f0056eff0ec7c1b1e088f71068eb4e47a46eb54f6c065c6ad7675/magic_filter-1.0.12-py3-none-any.whl"
    sha256 "e5929e544f310c2b1f154318db8c5cdf544dd658efa998172acd2e4ba0f6c6a6"
  end

  resource "multidict" do
    url "https://files.pythonhosted.org/packages/1a/c2/c2d94cbe6ac1753f3fc980da97b3d930efe1da3af3c9f5125354436c073d/multidict-6.7.1.tar.gz"
    sha256 "ec6652a1bee61c53a3e5776b6049172c53b6aaba34f18c9ad04f82712bac623d"
  end

  resource "propcache" do
    url "https://files.pythonhosted.org/packages/ec/44/c87281c333769159c50594f22610f77398a47ccbfbbf23074e744e86f87c/propcache-0.5.2.tar.gz"
    sha256 "01c4fc7480cd0598bb4b57022df55b9ca296da7fc5a8760bd8451a7e63a7d427"
  end

  resource "pydantic" do
    url "https://files.pythonhosted.org/packages/fd/7b/122376b1fd3c62c1ed9dc80c931ace4844b3c55407b6fb2d199377c9736f/pydantic-2.13.4-py3-none-any.whl"
    sha256 "45a282cde31d808236fd7ea9d919b128653c8b38b393d1c4ab335c62924d9aba"
  end

  resource "pydantic-core" do
    url "https://files.pythonhosted.org/packages/a7/74/319859f70c733f341df03823c8ca27ce9003faaac3ffa3110f3af1c8641a/pydantic_core-2.47.0.tar.gz"
    sha256 "422c1797a7864b2a9a996435aba92fe571fb80190f67a31edbc1ac040c7b51fe"
  end

  resource "pydantic-settings" do
    url "https://files.pythonhosted.org/packages/ae/8d/f1af3832f5e6eb13ba94ee809e72b8ecb5eef226d27ee0bef7d963d943c7/pydantic_settings-2.14.1-py3-none-any.whl"
    sha256 "6e3c7edfd8277687cdc598f56e5cff0e9bfff0910a3749deaa8d4401c3a2b9de"
  end

  resource "python-dotenv" do
    url "https://files.pythonhosted.org/packages/0b/d7/1959b9648791274998a9c3526f6d0ec8fd2233e4d4acce81bbae76b44b2a/python_dotenv-1.2.2-py3-none-any.whl"
    sha256 "1d8214789a24de455a8b8bd8ae6fe3c6b69a5e3d64aa8a8e5d68e694bbcb285a"
  end

  resource "python-socks" do
    url "https://files.pythonhosted.org/packages/15/fe/9a58cb6eec633ff6afae150ca53c16f8cc8b65862ccb3d088051efdfceb7/python_socks-2.8.1-py3-none-any.whl"
    sha256 "28232739c4988064e725cdbcd15be194743dd23f1c910f784163365b9d7be035"
  end

  resource "typing-extensions" do
    url "https://files.pythonhosted.org/packages/18/67/36e9267722cc04a6b9f15c7f3441c2363321a3ea07da7ae0c0707beb2a9c/typing_extensions-4.15.0-py3-none-any.whl"
    sha256 "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"
  end

  resource "typing-inspection" do
    url "https://files.pythonhosted.org/packages/dc/9b/47798a6c91d8bdb567fe2698fe81e0c6b7cb7ef4d13da4114b41d239f65d/typing_inspection-0.4.2-py3-none-any.whl"
    sha256 "4ed1cacbdc298c220f1bd249ed5287caa16f34d44ef4e9c3d0cbad5b521545e7"
  end

  resource "yarl" do
    url "https://files.pythonhosted.org/packages/79/12/1e8f37460ea0f7eb59c221fdaf0ed75e7ac43e97f8093b9c6f411df50a78/yarl-1.24.2.tar.gz"
    sha256 "9ac374123c6fd7abf64d1fec93962b0bd4ee2c19751755a762a72dd96c0378f8"
  end

  def install
    virtualenv_install_with_resources
  end

  def caveats
    <<~EOS
      必填环境变量：
        TG_BOT_TOKEN          Telegram Bot Token
        TG_ALLOWED_USER_IDS   允许的用户 ID（逗号分隔，或 * 允许所有用户）

      配置方式（二选一）：
        1) 进程环境变量：export TG_BOT_TOKEN=<token> TG_ALLOWED_USER_IDS=<ids>
        2) Env_File：在运行目录放置 .env，或使用 tg-cli-gateway --env-file /path/to/.env
      （同名配置项以进程环境变量为准）

      启动：tg-cli-gateway

      可选 tmux 终端模式：
        当 CLAUDE_TMUX_MODE=true 时，需要 PATH 中存在 tmux 可执行程序。
        未安装 tmux 时该模式不可用，但不影响默认模式运行。
    EOS
  end

  test do
    assert_match version.to_s, shell_output("#{bin}/tg-cli-gateway --version")
  end
end
