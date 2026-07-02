/**
 * LINE秘書 レシート記帳 - Google Apps Script
 *
 * 受信したレシートデータをスプレッドシートに追記し、
 * レシート画像をGoogle Driveに保存する（電子帳簿保存法対応）。
 *
 * セットアップ手順は「レシート記帳セットアップ手順.md」を参照。
 */

const SHEET_NAME = "経費";           // 記帳先シート名
const DRIVE_FOLDER_NAME = "レシート画像"; // 画像保存フォルダ名（Driveに自動作成）

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
  const root = getOrCreateFolder_(DriveApp.getRootFolder(), DRIVE_FOLDER_NAME);
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
