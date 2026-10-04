# 3. 指定フォルダへ移動して Python スクリプトを実行
$targetDir = "C:\miyaki\ses-matching"
Write-Host "作業ディレクトリ [$targetDir] に移動して main.py を実行します..." -ForegroundColor Green
Set-Location -Path $targetDir

python main.py