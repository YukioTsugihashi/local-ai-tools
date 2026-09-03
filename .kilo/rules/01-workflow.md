# 作業ルール

1. コード変更後は必ずテストを実行する(高速ループ、数秒で終わる)。
   コマンドは毎回新しいシェル(PowerShell 5.1)で実行される。`cd` は次の実行へ
   引き継がれず、`&&` は使えない。必ず `;` でつないだ1行で実行する:
   `cd text-processing-bridge; .\.venv\Scripts\python.exe -m pytest -q`
   - venvが壊れた場合の再構築(オフラインで完結する):
     `cd text-processing-bridge; & "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe" -m venv .venv`
     `cd text-processing-bridge; .\.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse-win -r requirements.txt`
     `cd text-processing-bridge; .\.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse-win -r requirements-office.txt`
2. ブリッジ(app/)やハブ(local_tool_bridge/)のコードを変えたら、
   ローカルサービスを再起動しないと反映されない:
   `Stop-ScheduledTask -TaskName minutes-pipeline-local-services; Start-ScheduledTask -TaskName minutes-pipeline-local-services`
   起動できたかは `http://localhost:8010/` と `logs/local_services.log` で確認する。
3. `.env` と `data/` には絶対に触らない(読み取りも不可)
4. 最小差分で編集する。依頼されていないリファクタ・整形をしない。特に禁止:
   - 関数をクラスへ包み替える
   - 関数を別ファイルへ移す・モジュール構成を変える
   - 公開関数のシグネチャを変える
   (タスクで明示的に指示された場合のみ可)
5. 変更が動作確認できたら日本語の簡潔なメッセージで `git commit` する。
   作業開始前にワーキングツリーがクリーンなことを確認する。
   ファイル・主要な関数・エンドポイントを追加/削除した場合は、コミット前に
   `.kilo/rules/02-map.md` と AGENTS.md の表を更新して同じコミットに含める
   `git add .` でまとめて入れない。`git status` と `git diff` で確かめ、その依頼で
   変えたファイルだけを `git add` する。コミット後は `git push local` で
   バックアップ先へ送る。
   作業を始める前に `git status` で現在のブランチを見る。意図と違うと思ったら
   手をつけずに知らせる。ブランチの切り替え・マージ・削除は人が行うため、
   自分では実行しない(`git switch -c` で新しい枝を作るのは可)。
   1つのコミットには1つの目的だけを入れる。戻せる最小単位がコミットなので、
   混ぜると片方だけを戻せなくなる。目的が二つ以上になったら、区切りがついた
   その場でコミットして分ける。まとめて書いてから切り分けようとしない。
   ただし実装とそのテスト、関数の追加と 02-map.md・AGENTS.md の追従は同じ
   コミットに入れる。`git add -p` でファイルの中を分けない——テストした内容と
   コミットされる内容が別物になるため、pre-commitが止める。
   手順の全体は GIT_BRANCH_WORKFLOW.md にある
6. `_patch*.py` のようなパッチスクリプトは書かない。ファイルを直接編集する
7. docstring・コメントは既存の文体に合わせる(である調。「何をするか」より
   「なぜそうするか」を書く)
8. どこに何があるかは `02-map.md` を見る。grepで探索する前にマップを確認する
9. 日本語を多く含む結果はコンソールに流さず、output/ へUTF-8ファイルとして
   書き出してからreadツールで読む。コンソールの日本語が文字化けしていても、
   それは表示だけの問題であり「データが無い・壊れている」と判断してはいけない。
   中身の判断は必ずファイル経由で行う
