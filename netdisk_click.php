<?php
/**
 * 网盘链接点击统计端点
 *
 * 用途：记录用户点击"夸克网盘转存"按钮的行为，用于分析哪些影片有转存意向
 * 部署：上传到网站根目录（与 api.php 同级）
 * 调用：页面按钮点击时 sendBeacon 上报 {vod_id, share_url}
 *
 * 注意：无敏感信息，只记 vod_id/时间/IP摘要，用于流量分析
 */

header('Content-Type: application/json; charset=utf-8');
header('Access-Control-Allow-Origin: *');

// 读取参数（支持 GET / POST 表单 / JSON body）
$vodId = intval($_GET['vod_id'] ?? 0);
$shareUrl = trim($_GET['share_url'] ?? '');
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    $raw = file_get_contents('php://input');
    $json = json_decode($raw, true);
    if (is_array($json)) {
        $vodId = intval($json['vod_id'] ?? $vodId);
        $shareUrl = trim($json['share_url'] ?? $shareUrl);
    } else {
        $vodId = intval($_POST['vod_id'] ?? $vodId);
        $shareUrl = trim($_POST['share_url'] ?? $shareUrl);
    }
}

if ($vodId <= 0 && empty($shareUrl)) {
    echo json_encode(['code' => 0, 'msg' => 'no data']);
    exit;
}

require_once __DIR__ . '/application/database.php';
try {
    $pdo = new PDO(
        "mysql:host={$db['hostname']};port=" . ($db['hostport'] ?: 3306) . ";dbname={$db['database']};charset=utf8mb4",
        $db['username'],
        $db['password'],
        [PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION, PDO::ATTR_TIMEOUT => 5]
    );
} catch (Exception $e) {
    echo json_encode(['code' => 0, 'msg' => 'db error']);
    exit;
}

// 首次调用自动建表
$pdo->exec("CREATE TABLE IF NOT EXISTS mac_netdisk_click (
    id         BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    vod_id     INT UNSIGNED NOT NULL DEFAULT 0,
    share_url  VARCHAR(500) DEFAULT '',
    ip_hash    VARCHAR(40)  DEFAULT '',
    referer    VARCHAR(500) DEFAULT '',
    click_time DATETIME     DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_vod (vod_id),
    INDEX idx_time (click_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='网盘链接点击统计'");

// IP 脱敏（只存哈希前16位，不存原始IP）
$ip = $_SERVER['REMOTE_ADDR'] ?? '';
$ipHash = substr(md5($ip . 'easysvip_salt'), 0, 16);
$referer = substr($_SERVER['HTTP_REFERER'] ?? '', 0, 500);

$stmt = $pdo->prepare("INSERT INTO mac_netdisk_click (vod_id, share_url, ip_hash, referer) VALUES (?, ?, ?, ?)");
$stmt->execute([$vodId, $shareUrl, $ipHash, $referer]);

echo json_encode(['code' => 1, 'msg' => 'ok']);
