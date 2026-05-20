-- =====================================================================
-- HDFC Securities Portfolio Demo - Seed Data
-- Realistic Indian equities + Indian customers to make the demo feel
-- like the customer's actual production environment.
-- =====================================================================
SET search_path TO portfolio, public;

-- ----------------------- Security Master (Nifty / popular stocks) ------
INSERT INTO security_master (security_id, isin, symbol, company_name, exchange, segment, sector, lot_size, face_value) VALUES
 (1001, 'INE002A01018', 'RELIANCE',   'Reliance Industries Ltd',           'NSE', 'EQ', 'Oil & Gas',           1, 10),
 (1002, 'INE467B01029', 'TCS',        'Tata Consultancy Services Ltd',     'NSE', 'EQ', 'Information Technology', 1, 1),
 (1003, 'INE009A01021', 'INFY',       'Infosys Ltd',                       'NSE', 'EQ', 'Information Technology', 1, 5),
 (1004, 'INE040A01034', 'HDFCBANK',   'HDFC Bank Ltd',                     'NSE', 'EQ', 'Banking',             1, 1),
 (1005, 'INE090A01021', 'ICICIBANK',  'ICICI Bank Ltd',                    'NSE', 'EQ', 'Banking',             1, 2),
 (1006, 'INE237A01028', 'KOTAKBANK',  'Kotak Mahindra Bank Ltd',           'NSE', 'EQ', 'Banking',             1, 5),
 (1007, 'INE585B01010', 'MARUTI',     'Maruti Suzuki India Ltd',           'NSE', 'EQ', 'Automobile',          1, 5),
 (1008, 'INE154A01025', 'ITC',        'ITC Ltd',                           'NSE', 'EQ', 'FMCG',                1, 1),
 (1009, 'INE062A01020', 'SBIN',       'State Bank of India',               'NSE', 'EQ', 'Banking',             1, 1),
 (1010, 'INE752E01010', 'POWERGRID',  'Power Grid Corp of India Ltd',      'NSE', 'EQ', 'Power',               1, 10),
 (1011, 'INE021A01026', 'ASIANPAINT', 'Asian Paints Ltd',                  'NSE', 'EQ', 'Paints',              1, 1),
 (1012, 'INE018A01030', 'LT',         'Larsen & Toubro Ltd',               'NSE', 'EQ', 'Construction',        1, 2),
 (1013, 'INE423A01024', 'ADANIENT',   'Adani Enterprises Ltd',             'NSE', 'EQ', 'Diversified',         1, 1),
 (1014, 'INE029A01011', 'BPCL',       'Bharat Petroleum Corp Ltd',         'NSE', 'EQ', 'Oil & Gas',           1, 10),
 (1015, 'INE066A01021', 'EICHERMOT',  'Eicher Motors Ltd',                 'NSE', 'EQ', 'Automobile',          1, 1),
 (1016, 'INE158A01026', 'HEROMOTOCO', 'Hero MotoCorp Ltd',                 'NSE', 'EQ', 'Automobile',          1, 2),
 (1017, 'INE860A01027', 'HCLTECH',    'HCL Technologies Ltd',              'NSE', 'EQ', 'Information Technology', 1, 2),
 (1018, 'INE075A01022', 'WIPRO',      'Wipro Ltd',                         'NSE', 'EQ', 'Information Technology', 1, 2),
 (1019, 'INE795G01014', 'HDFCLIFE',   'HDFC Life Insurance Co Ltd',        'NSE', 'EQ', 'Insurance',           1, 10),
 (1020, 'INE205A01025', 'VEDL',       'Vedanta Ltd',                       'NSE', 'EQ', 'Metals',              1, 1);

-- ----------------------- Customers (mix of segments) -------------------
INSERT INTO customer (customer_id, client_code, pan, full_name, email, phone, demat_account, risk_profile, segment, kyc_status) VALUES
 (10001, 'HS0010001', 'AAAPK1111A', 'Rajesh Kumar Sharma',  'rajesh.sharma@example.com',  '+91-9810010001', 'IN300484-10010001', 'MODERATE',    'RETAIL',        'VERIFIED'),
 (10002, 'HS0010002', 'BBBPK2222B', 'Priya Iyer',           'priya.iyer@example.com',     '+91-9820010002', 'IN300484-10010002', 'AGGRESSIVE',  'HNI',           'VERIFIED'),
 (10003, 'HS0010003', 'CCCPK3333C', 'Vikram Mehta',         'vikram.mehta@example.com',   '+91-9830010003', 'IN300484-10010003', 'AGGRESSIVE',  'UHNI',          'VERIFIED'),
 (10004, 'HS0010004', 'DDDPK4444D', 'Anita Desai',          'anita.desai@example.com',    '+91-9840010004', 'IN300484-10010004', 'CONSERVATIVE','RETAIL',        'VERIFIED'),
 (10005, 'HS0010005', 'EEEPK5555E', 'Suresh Reddy',         'suresh.reddy@example.com',   '+91-9850010005', 'IN300484-10010005', 'MODERATE',    'NRI',           'VERIFIED'),
 (10006, 'HS0010006', 'FFFPK6666F', 'Kavita Joshi',         'kavita.joshi@example.com',   '+91-9860010006', 'IN300484-10010006', 'MODERATE',    'RETAIL',        'VERIFIED'),
 (10007, 'HS0010007', 'GGGPK7777G', 'Arjun Nair',           'arjun.nair@example.com',     '+91-9870010007', 'IN300484-10010007', 'AGGRESSIVE',  'HNI',           'VERIFIED'),
 (10008, 'HS0010008', 'HHHPK8888H', 'Meera Krishnan',       'meera.k@example.com',        '+91-9880010008', 'IN300484-10010008', 'CONSERVATIVE','RETAIL',        'VERIFIED'),
 (10009, 'HS0010009', 'IIIPK9999I', 'Sandeep Gupta',        'sandeep.gupta@example.com',  '+91-9890010009', 'IN300484-10010009', 'MODERATE',    'RETAIL',        'VERIFIED'),
 (10010, 'HS0010010', 'JJJPK1010J', 'Neha Kapoor',          'neha.kapoor@example.com',    '+91-9811010010', 'IN300484-10010010', 'AGGRESSIVE',  'HNI',           'VERIFIED');

-- ----------------------- Market Prices (current LTP) -------------------
INSERT INTO market_price (security_id, ltp, prev_close, day_open, day_high, day_low, volume) VALUES
 (1001, 2945.50, 2920.00, 2925.00, 2952.00, 2918.50,  4_521_300),
 (1002, 4180.75, 4150.00, 4155.00, 4195.00, 4148.20,  1_872_400),
 (1003, 1865.30, 1840.00, 1845.00, 1872.00, 1841.50,  3_211_800),
 (1004, 1672.15, 1660.00, 1662.50, 1678.00, 1659.30,  6_785_200),
 (1005, 1245.00, 1230.50, 1232.00, 1248.00, 1231.20,  5_120_900),
 (1006, 1812.40, 1795.00, 1798.00, 1818.50, 1796.10,  1_645_700),
 (1007, 11250.00, 11180.00, 11200.00, 11295.00, 11175.00,  321_540),
 (1008,  457.25,  453.00,  454.00,  459.00,  452.80, 12_345_600),
 (1009,  812.30,  805.00,  806.50,  815.00,  804.20, 8_932_400),
 (1010,  308.75,  305.00,  305.50,  310.20,  304.80, 9_123_500),
 (1011, 2890.20, 2870.00, 2872.00, 2898.00, 2869.50,    524_300),
 (1012, 3625.00, 3590.00, 3595.00, 3635.00, 3588.50,  1_245_800),
 (1013, 2980.50, 2950.00, 2955.00, 2995.00, 2948.00,  1_854_200),
 (1014,  315.40,  312.00,  312.50,  317.00,  311.80,  5_421_700),
 (1015, 4780.25, 4750.00, 4755.00, 4795.00, 4748.50,    312_400),
 (1016, 4920.10, 4880.00, 4885.00, 4935.00, 4878.20,    421_800),
 (1017, 1620.50, 1605.00, 1608.00, 1625.00, 1604.50,  1_872_300),
 (1018,  295.75,  293.00,  293.50,  297.20,  292.80,  7_124_500),
 (1019,  712.25,  708.00,  709.00,  715.00,  707.80,  2_341_600),
 (1020,  420.50,  416.00,  417.00,  423.00,  415.50,  4_512_700);

-- ----------------------- Holdings (current portfolios) -----------------
-- Mixed portfolios across customers/segments
INSERT INTO holding (holding_id, customer_id, security_id, quantity, avg_buy_price, invested_value, last_trade_date) VALUES
 (20001, 10001, 1001, 50,   2800.00,  140000.00, '2025-11-12'),
 (20002, 10001, 1004, 30,   1600.00,   48000.00, '2025-12-03'),
 (20003, 10001, 1008, 200,   440.00,   88000.00, '2025-10-20'),
 (20004, 10002, 1002, 100,  4000.00,  400000.00, '2026-01-15'),
 (20005, 10002, 1003, 250,  1750.00,  437500.00, '2026-02-10'),
 (20006, 10002, 1007, 25,  10800.00,  270000.00, '2026-03-01'),
 (20007, 10003, 1001, 500,  2700.00, 1350000.00, '2025-09-05'),
 (20008, 10003, 1002, 300,  4050.00, 1215000.00, '2025-11-22'),
 (20009, 10003, 1004, 800,  1580.00, 1264000.00, '2025-12-15'),
 (20010, 10003, 1011, 200,  2750.00,  550000.00, '2026-01-08'),
 (20011, 10003, 1015, 50,   4600.00,  230000.00, '2026-02-25'),
 (20012, 10004, 1008, 500,   430.00,  215000.00, '2025-08-10'),
 (20013, 10004, 1010, 800,   295.00,  236000.00, '2025-09-18'),
 (20014, 10004, 1019, 300,   695.00,  208500.00, '2025-10-25'),
 (20015, 10005, 1002, 75,   4100.00,  307500.00, '2026-01-20'),
 (20016, 10005, 1017, 150,  1580.00,  237000.00, '2026-02-14'),
 (20017, 10005, 1018, 500,   285.00,  142500.00, '2026-03-05'),
 (20018, 10006, 1004, 25,   1640.00,   41000.00, '2025-11-30'),
 (20019, 10006, 1008, 100,   445.00,   44500.00, '2025-12-12'),
 (20020, 10006, 1009, 80,    795.00,   63600.00, '2026-01-25'),
 (20021, 10007, 1001, 200,  2820.00,  564000.00, '2025-10-30'),
 (20022, 10007, 1012, 100,  3550.00,  355000.00, '2025-12-20'),
 (20023, 10007, 1013, 150,  2900.00,  435000.00, '2026-02-18'),
 (20024, 10008, 1008, 300,   438.00,  131400.00, '2025-09-15'),
 (20025, 10008, 1010, 500,   298.00,  149000.00, '2025-11-05'),
 (20026, 10009, 1003, 60,   1810.00,  108600.00, '2026-01-10'),
 (20027, 10009, 1005, 100,  1215.00,  121500.00, '2026-02-22'),
 (20028, 10009, 1014, 800,   305.00,  244000.00, '2026-03-08'),
 (20029, 10010, 1006, 150,  1780.00,  267000.00, '2025-12-28'),
 (20030, 10010, 1016, 30,   4830.00,  144900.00, '2026-01-30'),
 (20031, 10010, 1020, 1000,  410.00,  410000.00, '2026-02-28');

-- ----------------------- Trades (historical) ---------------------------
INSERT INTO trade (trade_id, customer_id, security_id, side, quantity, price, trade_value, brokerage, order_id, exchange, executed_at) VALUES
 (30001, 10001, 1001, 'BUY',  50,  2800.00, 140000.00, 70.00,  'ORD-2025-1101-001', 'NSE', '2025-11-12 10:15:22'),
 (30002, 10002, 1002, 'BUY',  100, 4000.00, 400000.00, 200.00, 'ORD-2026-0115-002', 'NSE', '2026-01-15 09:32:10'),
 (30003, 10003, 1001, 'BUY',  500, 2700.00,1350000.00, 675.00, 'ORD-2025-0905-003', 'NSE', '2025-09-05 11:45:55'),
 (30004, 10002, 1003, 'BUY',  250, 1750.00, 437500.00, 218.75, 'ORD-2026-0210-004', 'NSE', '2026-02-10 14:22:08'),
 (30005, 10003, 1002, 'BUY',  300, 4050.00,1215000.00, 607.50, 'ORD-2025-1122-005', 'NSE', '2025-11-22 13:18:34'),
 (30006, 10004, 1008, 'BUY',  500,  430.00, 215000.00, 107.50, 'ORD-2025-0810-006', 'NSE', '2025-08-10 10:55:12'),
 (30007, 10005, 1017, 'BUY',  150, 1580.00, 237000.00, 118.50, 'ORD-2026-0214-007', 'NSE', '2026-02-14 11:30:45'),
 (30008, 10007, 1013, 'BUY',  150, 2900.00, 435000.00, 217.50, 'ORD-2026-0218-008', 'NSE', '2026-02-18 12:48:19'),
 (30009, 10010, 1020, 'BUY', 1000,  410.00, 410000.00, 205.00, 'ORD-2026-0228-009', 'NSE', '2026-02-28 09:42:33');

-- ----------------------- Sequences for live load script ----------------
CREATE SEQUENCE IF NOT EXISTS trade_id_seq    START WITH 30100;
CREATE SEQUENCE IF NOT EXISTS holding_id_seq  START WITH 20100;
