import pandas as pd
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status

class AttendanceAPIView(APIView):
    def post(self, request, format=None):
        # التأكد من وجود الملف في الطلب (Request)
        uploaded_file = request.FILES.get('file')
        if not uploaded_file:
            return Response({"error": "الرجاء رفع ملف البصمة (Excel أو CSV)."}, status=status.HTTP_400_BAD_REQUEST)
        
        try:
            # قراءة الملف تلقائياً بناءً على الامتداد
            if uploaded_file.name.endswith('.csv'):
                df = pd.read_csv(uploaded_file)
            else:
                df = pd.read_excel(uploaded_file)
                
            # تنظيف أسماء الأعمدة
            df.columns = df.columns.str.strip()
            
            # التعرف الذكي على الأعمدة
            emp_col = next((col for col in df.columns if any(k in col.lower() for k in ['emp', 'id', 'user', 'كود'])), None)
            time_col = next((col for col in df.columns if any(k in col.lower() for k in ['time', 'date', 'stamp', 'تاريخ', 'وقت'])), None)
            
            if not emp_col or not time_col:
                return Response({"error": f"تعذر التعرف على أعمدة الكود أو الوقت. الأعمدة المتاحة: {list(df.columns)}"}, status=status.HTTP_400_BAD_REQUEST)
                
            df = df.rename(columns={emp_col: 'Employee_ID', time_col: 'Timestamp'})
            df['Timestamp'] = pd.to_datetime(df['Timestamp'])
            df['Date'] = df['Timestamp'].dt.date
            
            # تحليل وتوزيع الحضور والانصراف لكل موظف يومياً
            daily_summary = df.groupby(['Employee_ID', 'Date']).agg(
                First_Check_In=('Timestamp', 'min'),
                Last_Check_Out=('Timestamp', 'max'),
                Total_Punches=('Timestamp', 'count')
            ).reset_index()
            
            # تحديد الحالة
            daily_summary['Status'] = 'منتظم'
            daily_summary.loc[daily_summary['Total_Punches'] == 1, 'Status'] = 'بصمة مفردة (تحتاج مراجعة)'
            
            # حساب ساعات العمل
            mask = daily_summary['Total_Punches'] > 1
            daily_summary['Work_Hours'] = 0.0
            daily_summary.loc[mask, 'Work_Hours'] = (
                daily_summary.loc[mask, 'Last_Check_Out'] - daily_summary.loc[mask, 'First_Check_In']
            ).dt.total_seconds() / 3600
            
            # تنسيق النتائج
            daily_summary['Check_In'] = daily_summary['First_Check_In'].dt.strftime('%H:%M:%S')
            daily_summary['Check_Out'] = daily_summary.apply(
                lambda row: row['Last_Check_Out'].strftime('%H:%M:%S') if row['Total_Punches'] > 1 else 'غير متوفر', axis=1
            )
            daily_summary['Work_Hours'] = daily_summary['Work_Hours'].round(2)
            daily_summary['Date'] = daily_summary['Date'].astype(str)
            
            output_columns = ['Employee_ID', 'Date', 'Check_In', 'Check_Out', 'Work_Hours', 'Total_Punches', 'Status']
            final_data = daily_summary[output_columns].to_dict(orient='records')
            
            return Response({
                "status": "success",
                "total_records": len(final_data),
                "data": final_data
            }, status=status.HTTP_200_OK)
            
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
