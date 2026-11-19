// 画面とバックエンド(src/remi/server/api.py の @rpc)の間で受け渡すデータの形(設計書 §2.6)。
// 実行時には何もしない。各ファイルから `/** @typedef {import("./types.js").ConversationData} ConversationData */`
// のように参照する。バックエンドの戻り値の項目を変えたら、ここも直すこと
// (主な型の項目名は tests/test_types_doc.py で、実際の戻り値と一致するかを確認している)。

/**
 * getTree の戻り値。
 * @typedef {object} TreeData
 * @property {number} unread              すべてのサーバーの未読数
 * @property {TreeServer[]} servers
 * @property {Relation[]} relationsInUse   どこかのプロジェクトで表示している関係(一覧の絞り込みボタンを決める)
 */

/**
 * @typedef {object} TreeServer
 * @property {number} serverId
 * @property {string} name
 * @property {string} url
 * @property {"ok"|"error"|"unknown"} status
 * @property {string|null} error           エラーコード(errors.* で翻訳する)
 * @property {string|null} lastSyncedAt    ISO 8601
 * @property {number} unread
 * @property {number} count
 * @property {boolean} showAllProjects    チケットのないプロジェクトも表示するか(サーバーごと)
 * @property {Relation[]} relations        表示するチケットの既定(プロジェクトで設定していなければこれを使う)
 * @property {boolean} relationsOwn        表示するチケットの既定を、すべてから変えているか
 * @property {TreeProject[]} projects
 */

/**
 * @typedef {object} TreeProject
 * @property {number} projectId
 * @property {string} name
 * @property {boolean} accessible          自分がメンバーでない(チケット経由で知った)プロジェクトは false
 * @property {boolean} active
 * @property {number} unread               子プロジェクトを含む
 * @property {number} count                子プロジェクトを含む
 * @property {Relation[]} relations        表示するチケット(親プロジェクト・サーバーの既定からの継承を解決済み)
 * @property {boolean} relationsOwn        表示するチケットを、このプロジェクト自身で設定しているか
 * @property {TreeVersion[]} versions      ウォッチしているバージョン(期日順)
 * @property {TreeProject[]} children
 */

/**
 * ツリーに出すバージョン(ロードマップの監視)。
 * @typedef {object} TreeVersion
 * @property {number} versionId
 * @property {string} name
 * @property {"open"|"locked"|"closed"} status
 * @property {boolean} closed              終了したバージョン(ツリーでは畳んだ「終了したバージョン」の下に出す)
 * @property {string|null} dueDate         期日(YYYY-MM-DD)
 * @property {number} done                 完了したチケットの数
 * @property {number} total                チケットの数
 * @property {boolean} changed             まだ見ていない変化があるか
 * @property {boolean} synced              チケットの一覧を取得済みか(まだなら件数を「…」で出す。終了したものは開いたときに取得する)
 */

/**
 * getVersion の戻り値。
 * @typedef {object} VersionData
 * @property {number} serverId
 * @property {number} versionId
 * @property {string} name
 * @property {"open"|"locked"|"closed"} status
 * @property {string|null} dueDate
 * @property {string} description
 * @property {number} projectId
 * @property {string[]} breadcrumb         プロジェクトの階層
 * @property {number} done
 * @property {number} total
 * @property {string} url                  Redmineのバージョンのページ
 * @property {VersionEvent[]} events       古い順
 * @property {boolean} synced              チケットの一覧を取得済みか(終了したバージョンは開いたときに取得する)
 */

/**
 * @typedef {object} VersionEvent
 * @property {number} eventId
 * @property {"created"|"added"|"removed"|"closed"|"reopened"|"due_date"|"status"|"name"} kind
 * @property {number|null} issueId
 * @property {string|null} subject
 * @property {string|null} oldValue
 * @property {string|null} newValue
 * @property {string} createdAt            ISO 8601
 * @property {boolean} unread              まだ見ていない変化か
 */

/**
 * listIssues の戻り値 `{issues: IssueSummary[], total: number}` の1件。
 * @typedef {object} IssueSummary
 * @property {number} serverId
 * @property {string} serverName
 * @property {number} issueId
 * @property {number} projectId
 * @property {string} subject
 * @property {string} status               ステータス名
 * @property {boolean} closed
 * @property {string|null} assignee
 * @property {string|null} updatedOn       ISO 8601
 * @property {number} unread
 * @property {Relation[]} relations
 * @property {boolean} monitored           関係が外れたチケット(クローズ済みも表示のとき)は false
 * @property {string|null} lastNoteUser
 * @property {string} lastNote             最後のコメントの先頭(改行は空白に置き換え済み)
 */

/** @typedef {"assigned"|"group"|"watching"|"authored"} Relation 自分との関係(group はグループ経由の担当) */

/**
 * getConversation の戻り値。
 * @typedef {object} ConversationData
 * @property {number} serverId
 * @property {string} serverName
 * @property {number} issueId
 * @property {string} subject
 * @property {string} url                  RedmineでのチケットのURL
 * @property {string[]} breadcrumb         親から順のプロジェクト名
 * @property {string} tracker
 * @property {string} status
 * @property {boolean} closed
 * @property {string} priority
 * @property {string|null} assignee
 * @property {string|null} author
 * @property {number|null} authorId
 * @property {number|null} statusId
 * @property {number|null} priorityId
 * @property {number|null} assignedToId
 * @property {Relation[]} relations
 * @property {boolean} watching
 * @property {string|null} createdOn
 * @property {string|null} updatedOn
 * @property {string} descriptionHtml      サニタイズ済みのHTML(空なら説明なし)
 * @property {Journal[]} journals          新しいものから limit 件を、古い順に並べたもの
 * @property {number} total                journal の総数
 * @property {boolean} hasMore             過去分が残っているか
 * @property {number} lastReadJournalId
 * @property {boolean} journalsSynced      履歴をまだ取得していなければ false
 * @property {"common_mark"|"textile"|"plain"} textFormat
 * @property {number|null} myUserId
 * @property {{body: string, private: boolean, failed: boolean}} draft
 */

/**
 * @typedef {object} Journal
 * @property {number} journalId
 * @property {string} notes                本文(書式のまま。編集に使う)
 * @property {number|null} userId
 * @property {string|null} userName
 * @property {boolean} mine
 * @property {boolean} private
 * @property {string|null} createdOn
 * @property {string} html                 コメントのHTML(サニタイズ済み)。コメントがなければ空
 * @property {JournalDetail[]} details     項目の変更・添付の追加など
 * @property {boolean} unread
 */

/**
 * 変更履歴の1行(src/remi/render/details.py)。
 * @typedef {object} JournalDetail
 * @property {string} kind                 "attr"、"attachment"、"attachment-removed"、"relation" など
 * @property {string} text                 表示する文(翻訳済み)
 * @property {number} [attachmentId]
 * @property {string} [filename]
 * @property {number} [issueId]            関係の相手
 * @property {string} [url]                添付の表示用URL(/attachments/...)
 * @property {boolean} [isImage]
 * @property {number|null} [filesize]
 */

/**
 * getEditOptions の戻り値(ヘッダで変更できる項目の選択肢)。
 * @typedef {object} EditOptions
 * @property {{id: number, name: string, closed: boolean}[]} statuses   ワークフロー上遷移できるもの
 * @property {boolean} statusesExact       false なら Redmine 5.0 未満ですべてのステータスを出している
 * @property {{id: number, name: string, group: boolean}[]} assignees
 * @property {{id: number, name: string}[]} priorities
 * @property {number|null} me
 * @property {{status_id: number|null, assigned_to_id: number|null, priority_id: number|null}} current
 * @property {WritePermissions} permissions  書き込めるか(できない操作の入力欄は無効にする)
 */

/**
 * このチケットに書き込めるか(getEditOptions の permissions)。権限が分からないときはすべて true。
 * @typedef {object} WritePermissions
 * @property {boolean} editFields          ステータス・担当者・優先度を変えられる
 * @property {boolean} changeStatus        ステータスを変えられる(ワークフロー上の遷移先がない場合は false)
 * @property {boolean} comment             コメントを書ける
 * @property {boolean} privateNotes        プライベートコメントにできる
 * @property {boolean} attach              ファイルを添付できる
 * @property {"all"|"own"|"none"} editNotes  コメントを編集・削除できる(すべて / 自分のものだけ / できない)
 * @property {"project_closed"|"no_permission"|null} reason  何も書き込めない理由
 */

/**
 * ヘッダ・「詳細」で変更できる項目。カスタムフィールドは `cf:<ID>`
 * @typedef {"status_id"|"assigned_to_id"|"priority_id"|"tracker_id"|"category_id"|"fixed_version_id"|"parent_issue_id"|"start_date"|"due_date"|"done_ratio"|"estimated_hours"|string} FieldKey
 */

/**
 * ヘッダで選んだ、送信前の変更(画面の中だけで使う)。
 * @typedef {object} FieldChange
 * @property {number|string|string[]|null} value
 * @property {string} label                選んだ値の表示名
 * @property {string} from                 元の値の表示名
 * @property {string|null} [name]          項目名(カスタムフィールドのとき)
 */

/**
 * getIssueDetails の戻り値(「詳細」の項目)。
 * @typedef {object} IssueDetails
 * @property {DetailField[]} fields
 * @property {boolean} customFieldsKnown   カスタムフィールドの形式が分かるか(管理者でなければ false)
 */

/**
 * 「詳細」の項目1つ。
 * @typedef {object} DetailField
 * @property {FieldKey} key
 * @property {"select"|"multiselect"|"date"|"issue"|"int"|"float"|"text"} type
 * @property {number|string|string[]|null} value  今の値(カスタムフィールドは文字列、複数選択は文字列のリスト)
 * @property {string} display              今の値の表示名
 * @property {{value: number|string|null, label: string}[]} [options]  選択肢(select・multiselect)
 * @property {boolean} editable            ここで変更できる形式か(書き込めるかは permissions で判断する)
 * @property {string} [label]              項目名(カスタムフィールド)
 * @property {boolean} [multiple]          複数選択のカスタムフィールド
 * @property {string|null} [format]        カスタムフィールドの形式(Redmineの field_format)
 * @property {string} [reason]             変更できない理由(custom_field_unknown / custom_field_unsupported)
 */

/**
 * /upload の戻り値と、送信前の添付の状態(画面の中だけで使う項目を含む)。
 * @typedef {object} Upload
 * @property {symbol} key
 * @property {string} filename
 * @property {boolean} isImage
 * @property {"uploading"|"done"|"error"} state
 * @property {string} [error]              失敗の理由(表示用)
 * @property {number} [id]                 /pending/{id} で画像を表示するための番号
 * @property {string} [token]              Redmineのアップロードトークン
 * @property {string} [contentType]
 * @property {number} [size]
 * @property {string} [url]
 */

/**
 * サーバーからのイベント(/events、SSE)。type ごとの項目は main.js の onEvent を参照。
 * @typedef {{type: string, serverId?: number, issueId?: number, issueIds?: number[], source?: string,
 *            error?: string|null, detail?: string}} ServerEvent
 */

export {};
