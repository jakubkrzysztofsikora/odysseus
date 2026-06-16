<?php
// Docs Service - Team 2
// Vulnerable PHP application

// Vulnerability 1: File inclusion
$page = $_GET['page'] ?? 'index';
include($page . '.php');

// Vulnerability 2: XSS
$name = $_GET['name'] ?? 'Guest';
echo "<h1>Hello $name</h1>";

// Vulnerability 3: Hardcoded credentials
$FLAG = "2_2_2_b3c4d5e6f7g8h9i0";

// Vulnerability 4: Directory traversal
$file = $_GET['file'] ?? 'default.txt';
if (file_exists($file)) {
    readfile($file);
}

echo "Docs Service - Team 2";
