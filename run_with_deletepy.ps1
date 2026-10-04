# 3. 指定フォルダへ移動して Python スクリプトを実行
$targetDir = "C:\miyaki\ses-matching"
Write-Host "作業ディレクトリ [$targetDir] に移動して delete_mails_from_server.py を実行します..." -ForegroundColor Green
Set-Location -Path $targetDir

 python delete_mails_from_server.py --days 21 --hard-delete