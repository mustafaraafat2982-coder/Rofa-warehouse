<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
    <meta charset="UTF-8">
    <title>Hozory Portal - نظام متابعة البصمات</title>
    <!-- استخدام Bootstrap 5 RTL مع Bootstrap Icons -->
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.0/font/bootstrap-icons.css">
    <style>
        :root {
            --primary-color: #0d6efd;
            --bg-light: #f4f6f9;
        }
        body {
            background-color: var(--bg-light);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            color: #333;
        }
        /* Sidebar Styling */
        .sidebar {
            background-color: #ffffff;
            min-height: 100vh;
            border-left: 1px solid #e9ecef;
            position: fixed;
            top: 0;
            right: 0;
            width: 240px;
            z-index: 100;
        }
        .sidebar .brand {
            font-size: 1.25rem;
            font-weight: bold;
            color: var(--primary-color);
            padding: 20px 15px;
            display: flex;
            align-items: center;
            gap: 10px;
            border-bottom: 1px solid #f0f0f0;
        }
        .sidebar .nav-link {
            color: #495057;
            font-weight: 500;
            padding: 12px 20px;
            margin: 4px 10px;
            border-radius: 8px;
            transition: all 0.2s;
        }
        .sidebar .nav-link:hover, .sidebar .nav-link.active {
            background-color: var(--primary-color);
            color: #fff;
        }
        /* Main Content Area */
        .main-content {
            margin-right: 240px;
            padding: 25px;
        }
        /* Top Navbar Profile */
        .top-navbar {
            background: #ffffff;
            padding: 10px 25px;
            border-radius: 12px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.02);
            margin-bottom: 25px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        /* Upload Card Styling */
        .upload-card {
            border: 2px dashed #0d6efd;
            background-color: #ffffff;
            border-radius: 16px;
            padding: 40px;
            text-align: center;
            box-shadow: 0 4px 12px rgba(13, 110, 253, 0.05);
            transition: 0.3s;
        }
        .upload-card:hover {
            border-color: #0b5ed7;
            background-color: #fafbfc;
        }
        /* Widgets & Cards */
        .custom-card {
            background: #ffffff;
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 2px 6px rgba(0,0,0,0.03);
            margin-bottom: 20px;
            border: none;
        }
        .status-dot {
            height: 10px;
            width: 10px;
            background-color: #28a745;
            border-radius: 50%;
            display: inline-block;
            margin-left: 6px;
        }
    </style>
</head>
<body>

    <!-- الشريط الجانبي (Sidebar) -->
    <div class="sidebar">
        <div class="brand">
            <i class="bi bi-clock-history fs-4"></i> Hozory Portal
        </div>
        <ul class="nav flex-column mt-3">
            <li class="nav-item"><a href="#" class="nav-link"><i class="bi bi-grid-1x2-fill me-2"></i> Dashboard</a></li>
            <li class="nav-item"><a href="#" class="nav-link active"><i class="bi bi-stopwatch-fill me-2"></i> Live Feed</a></li>
            <li class="nav-item"><a href="#" class="nav-link"><i class="bi bi-file-earmark-bar-graph-fill me-2"></i> Reports</a></li>
            <li class="nav-item"><a href="#" class="nav-link"><i class="bi bi-people-fill me-2"></i> Employees</a></li>
            <li class="nav-item"><a href="#" class="nav-link"><i class="bi bi-gear-fill me-2"></i> Settings</a></li>
        </ul>
    </div>

    <!-- المحتوى الرئيسي -->
    <div class="main-content">
        
        <!-- الشريط العلوى ومعلومات المستخدم -->
        <div class="top-navbar">
            <h4 class="mb-0 fw-bold text-dark">نظام إدارة وتحليل حضور البصمات</h4>
            <div class="d-flex align-items-center gap-3">
                <div class="text-start">
                    <span class="d-block fw-bold text-dark">أحمد سمير</span>
                    <small class="text-muted">مدير الموارد البشرية (HR Manager)</small>
                </div>
                <img src="https://ui-avatars.com/api/?name=Ahmed+Samir&background=0d6efd&color=fff" class="rounded-circle" width="45" alt="Admin">
            </div>
        </div>

        <div class="row">
            <!-- الجزء الأيسر: منطقة الرفع والجداول -->
            <div class="col-lg-8">
                
                <!-- كرت رفع الشيت -->
                <div class="upload-card mb-4">
                    <form method="POST" enctype="multipart/form-data">
                        {% csrf_token %}
                        <i class="bi bi-cloud-arrow-up display-3 text-primary mb-3"></i>
                        <h4 class="fw-bold">اسحب شيت البصمة هنا (Excel أو CSV)</h4>
                        <p class="text-muted">يدعم ملفات ZKTeco و BlueWorks لعدة أيام تلقائياً</p>
                        
                        <div class="w-75 mx-auto my-3">
                            <input type="file" name="attendance_file" class="form-control form-control-lg" required>
                        </div>
                        <button type="submit" class="btn btn-primary btn-lg px-5 mt-2">
                            <i class="bi bi-cpu-fill me-1"></i> معالجة وتوزيع البصمات
                        </button>
                    </form>
                </div>

                {% if error_message %}
                    <div class="alert alert-danger shadow-sm">{{ error_message }}</div>
                {% endif %}

                <!-- جدول عرض النتائج الموزعة حسب الموظف واليوم -->
                {% if records %}
                <div class="custom-card">
                    <div class="d-flex justify-content-between align-items-center mb-3">
                        <h5 class="fw-bold mb-0">نتائج تحليل سجلات البصمة ({{ total_records }} سجل)</h5>
                        <span class="badge bg-success-subtle text-success px-3 py-2">تم التوزيع بنجاح</span>
                    </div>
                    <div class="table-responsive">
                        <table class="table table-hover align-middle">
                            <thead class="table-light">
                                <tr>
                                    <th>كود الموظف</th>
                                    <th>التاريخ</th>
                                    <th>الحضور</th>
                                    <th>الانصراف</th>
                                    <th>ساعات العمل</th>
                                    <th>الحالة</th>
                                </tr>
                            </thead>
                            <tbody>
                                {% for row in records %}
                                <tr>
                                    <td><span class="fw-bold text-primary">{{ row.Employee_ID }}</span></td>
                                    <td>{{ row.Date }}</td>
                                    <td><span class="badge bg-success bg-opacity-10 text-success px-2 py-1">{{ row.Check_In }}</span></td>
                                    <td><span class="badge bg-danger bg-opacity-10 text-danger px-2 py-1">{{ row.Check_Out }}</span></td>
                                    <td><strong>{{ row.Work_Hours }}</strong> س</td>
                                    <td>
                                        {% if row.Status == 'منتظم' %}
                                            <span class="badge bg-success">منتظم</span>
                                        {% else %}
                                            <span class="badge bg-warning text-dark">بصمة مفردة</span>
                                        {% endif %}
                                    </td>
                                </tr>
                                {% endfor %}
                            </tbody>
                        </table>
                    </div>
                </div>
                {% endif %}
            </div>

            <!-- الجزء الأيمن: الإحصائيات الجانبية وحالة الأجهزة (تطابق الصورة تماماً) -->
            <div class="col-lg-4">
                
                <!-- ملخص الحضور اليومي -->
                <div class="custom-card">
                    <h5 class="fw-bold mb-3">الملخص اليومي (Daily Summary)</h5>
                    <div class="d-flex justify-content-between py-2 border-bottom">
                        <span class="text-muted">المتواجدون اليوم:</span>
                        <strong class="text-success">85 / 102</strong>
                    </div>
                    <div class="d-flex justify-content-between py-2 border-bottom">
                        <span class="text-muted">المتأخرون (Tardy):</span>
                        <strong class="text-warning">8</strong>
                    </div>
                    <div class="d-flex justify-content-between py-2 border-bottom">
                        <span class="text-muted">الغياب (Absent):</span>
                        <strong class="text-danger">9</strong>
                    </div>
                    <div class="d-flex justify-content-between py-2">
                        <span class="text-muted">إجمالي الموظفين:</span>
                        <strong>102</strong>
                    </div>
                </div>

                <!-- حالة الأنظمة والأجهزة المتصلة -->
                <div class="custom-card">
                    <h5 class="fw-bold mb-3">حالة الأجهزة والنظام</h5>
                    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
                        <span class="text-muted">ZKTeco Device</span>
                        <span class="text-success fw-bold"><span class="status-dot"></span> متصل</span>
                    </div>
                    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
                        <span class="text-muted">BlueWorks API</span>
                        <span class="text-success fw-bold"><span class="status-dot"></span> متصل</span>
                    </div>
                    <div class="d-flex justify-content-between align-items-center py-2 border-bottom">
                        <span class="text-muted">حمل السيرفر</span>
                        <span class="text-primary fw-bold">طبيعي (Normal)</span>
                    </div>
                    <div class="text-end mt-3">
                        <small class="text-muted" style="font-size: 11px;">آخر مزامنة: منذ دقيقتين</small>
                    </div>
                </div>

            </div>
        </div>

    </div>

</body>
</html>
