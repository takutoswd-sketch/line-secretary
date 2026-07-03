/**
 * LINE秘書 レシート記帳 - Google Apps Script
 *
 * 受信したレシートデータをスプレッドシートに追記し、
 * レシート画像をGoogle Driveに保存する（電子帳簿保存法対応）。
 *
 * セットアップ手順は「レシート記帳セットアップ手順.md」を参照。
 */

const SHEET_NAME = "経費";           // 記帳先シート名
const DRIVE_FOLDER_NAME = "レシート画像"; // 画像保存フォルダ名（経費フォルダ内に自動作成）
const EXPENSE_PARENT_ID = "1qQgTGANItb149jb-IICENpAfGH5EA6uP"; // マイドライブ/siki archidesign/経費

function doPost(e) {
  try {
    const data = JSON.parse(e.postData.contents);

    // 1. 画像をDriveに保存
    let imageUrl = "";
    if (data.image_base64) {
      imageUrl = saveImage_(data);
    }

    // 2. シートに追記
    const ss = SpreadsheetApp.getActiveSpreadsheet();
    let sheet = ss.getSheetByName(SHEET_NAME);
    if (!sheet) {
      sheet = ss.insertSheet(SHEET_NAME);
    }

    // ヘッダーがなければ作成
    if (sheet.getLastRow() === 0) {
      sheet.appendRow([
        "日付", "店名", "金額(税込)", "うち消費税", "勘定科目",
        "支払方法", "摘要", "読取精度", "画像リンク", "登録日時"
      ]);
      sheet.getRange(1, 1, 1, 10).setFontWeight("bold").setBackground("#e8f0fe");
      sheet.setFrozenRows(1);
    }

    sheet.appendRow([
      data.date || "",
      data.store || "",
      data.total || "",
      data.tax || "",
      data.category || "",
      data.payment || "",
      data.items || "",
      data.confidence || "",
      imageUrl,
      Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd HH:mm:ss")
    ]);

    const row = sheet.getLastRow();

    return ContentService
      .createTextOutput(JSON.stringify({ status: "ok", row: row, image_url: imageUrl }))
      .setMimeType(ContentService.MimeType.JSON);

  } catch (err) {
    return ContentService
      .createTextOutput(JSON.stringify({ status: "error", message: String(err) }))
      .setMimeType(ContentService.MimeType.JSON);
  }
}

/** レシート画像をDriveの「レシート画像/年」フォルダに保存し、URLを返す */
function saveImage_(data) {
  const root = getOrCreateFolder_(DriveApp.getFolderById(EXPENSE_PARENT_ID), DRIVE_FOLDER_NAME);
  const year = (data.date || "").substring(0, 4) || String(new Date().getFullYear());
  const yearFolder = getOrCreateFolder_(root, year);

  const ext = (data.media_type || "image/jpeg").includes("png") ? "png" : "jpg";
  const name = [
    data.date || Utilities.formatDate(new Date(), "Asia/Tokyo", "yyyy-MM-dd"),
    data.store || "不明",
    data.total ? data.total + "円" : ""
  ].filter(Boolean).join("_") + "." + ext;

  const blob = Utilities.newBlob(
    Utilities.base64Decode(data.image_base64),
    data.media_type || "image/jpeg",
    name
  );
  const file = yearFolder.createFile(blob);
  return file.getUrl();
}

function getOrCreateFolder_(parent, name) {
  const it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}

/**
 * 一度だけ手動実行: 既存の経費帳スプレッドシートとレシート画像フォルダを
 * マイドライブ直下 → siki archidesign/経費 へ移動する
 */
function migrateReceiptData() {
  const dest = DriveApp.getFolderById(EXPENSE_PARENT_ID);
  // スプレッドシート本体
  const ssFile = DriveApp.getFileById(SpreadsheetApp.getActiveSpreadsheet().getId());
  ssFile.moveTo(dest);
  console.log("経費帳を移動:", ssFile.getName());
  // レシート画像フォルダ（マイドライブ直下にある場合）
  const it = DriveApp.getRootFolder().getFoldersByName(DRIVE_FOLDER_NAME);
  while (it.hasNext()) {
    const f = it.next();
    f.moveTo(dest);
    console.log("フォルダを移動:", f.getName());
  }
  console.log("移動完了");
}

// ─────────────────────────────────────────
// LINE送信キュー（毎分トリガーで実行）
// スケジュールタスクがDriveの「LINE送信キュー」フォルダに置いた
// テキストファイルをLINEにpushして「LINE送信済み」フォルダへ移動する
// 必要なスクリプトプロパティ: LINE_TOKEN, LINE_USER_ID
// ─────────────────────────────────────────

const QUEUE_FOLDER_ID = "1xULMkqx-vVmgWMiDUVrYzg73Jkj5U4Rt"; // LINE送信キュー
const SENT_FOLDER_NAME = "LINE送信済み";

function sendQueuedLineMessages() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty("LINE_TOKEN");
  const uid = props.getProperty("LINE_USER_ID");
  if (!token || !uid) {
    console.error("LINE_TOKEN / LINE_USER_ID がスクリプトプロパティに未設定");
    return;
  }
  const queue = DriveApp.getFolderById(QUEUE_FOLDER_ID);
  const sent = getOrCreateFolder_(DriveApp.getRootFolder(), SENT_FOLDER_NAME);
  const files = queue.getFiles();
  while (files.hasNext()) {
    const f = files.next();
    try {
      let text = "";
      const mime = f.getMimeType();
      if (mime === "application/vnd.google-apps.document") {
        text = DocumentApp.openById(f.getId()).getBody().getText().trim();
      } else {
        text = f.getBlob().getDataAsString("UTF-8").trim();
      }
      if (text) {
        const res = UrlFetchApp.fetch("https://api.line.me/v2/bot/message/push", {
          method: "post",
          contentType: "application/json",
          headers: { Authorization: "Bearer " + token },
          payload: JSON.stringify({ to: uid, messages: [{ type: "text", text: text.substring(0, 4900) }] }),
          muteHttpExceptions: true
        });
        console.log("LINE push:", f.getName(), res.getResponseCode());
      }
      f.moveTo(sent);
    } catch (err) {
      console.error("送信失敗:", f.getName(), err);
      f.setName("ERROR_" + f.getName());
      f.moveTo(sent);
    }
  }
}
