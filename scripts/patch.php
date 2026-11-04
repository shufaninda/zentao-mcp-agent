<?php
// patch.php <file> <apply|revert> <find_b64> <replace_b64>
// apply: 首次备份 .orig，替换第一处 find -> replace
// revert: 用 .orig 还原
$file = $argv[1]; $mode = $argv[2];
$orig = $file . ".orig";
if ($mode === "revert") {
    if (!file_exists($orig)) { fwrite(STDERR, "no backup\n"); exit(1); }
    copy($orig, $file);
    exit(0);
}
$find = base64_decode($argv[3]); $repl = base64_decode($argv[4]);
if (!file_exists($orig)) copy($file, $orig);
$s = file_get_contents($file);
$pos = strpos($s, $find);
if ($pos === false) { fwrite(STDERR, "find-not-found\n"); exit(2); }
file_put_contents($file, substr_replace($s, $repl, $pos, strlen($find)));
echo "patched\n";
