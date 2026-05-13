# BingX MA 篩選器

即時從 BingX 讀取 K 線數據，依均線位置篩選所有交易對。

## 功能

- 即時抓取 BingX 所有現貨交易對
- 支援 8 種時間週期（1分鐘 ~ 週線）
- 支援 SMA / EMA 均線
- 自訂 MA 週期（2~200）
- 自動分類「均線之上」與「均線之下」
- 每個縮圖顯示該時間週期的 K 線 + MA 線
- 點擊縮圖查看詳細大圖
- 多種排序方式（成交量、漲跌幅、名稱）

## 安裝與執行

### 需求
- Python 3.8 以上

### Mac / Linux

```bash
chmod +x 啟動.sh
./啟動.sh
```

或直接：

```bash
pip3 install flask requests
python3 server.py
```

### Windows

雙擊 `啟動.bat`

或在命令提示字元：

```cmd
pip install flask requests
python server.py
```

啟動後瀏覽器會自動開啟 `http://127.0.0.1:5678`

## 使用方式

1. 選擇時間週期
2. 輸入 MA 週期（預設 20）
3. 選擇 MA 類型（SMA 或 EMA）
4. 選擇報價幣種（USDT / USDC）
5. 設定掃描數量（最多 150 個）
6. 點擊「開始掃描」

掃描完成後：
- 綠色頂部邊框 = 收盤價在均線之上
- 紅色頂部邊框 = 收盤價在均線之下
- 點擊任一卡片可查看詳細圖表

## 注意事項

- 掃描時間依網路速度而定，約 10~30 秒
- 使用公開 API，無需 API Key
- 資料來源：BingX 公開市場數據 API
