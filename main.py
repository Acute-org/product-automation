import http
from camoufox.sync_api import Camoufox

with Camoufox(humanize=True, window=(412, 892), fonts=['Noto Sans KR'], config={
  'headers.Accept-Encoding': 'gzip, deflate, br',
}) as browser:
  page = browser.new_page()
  page.goto("https://is-an.ai")

  page.wait_for_event('12312322')