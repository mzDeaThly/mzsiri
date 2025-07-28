# 1. ใช้ Python 3.10 เป็น base image
FROM python:3.10-slim

# 2. ตั้งค่า working directory ภายใน container
WORKDIR /app

# 3. ติดตั้ง FFmpeg ซึ่งจำเป็นสำหรับการเล่นเสียง
RUN apt-get update && apt-get install -y ffmpeg

# 4. คัดลอกไฟล์ requirements.txt เข้าไปใน container
COPY requirements.txt .

# 5. ติดตั้งไลบรารี Python ที่ระบุไว้
RUN pip install --no-cache-dir -r requirements.txt

# 6. คัดลอกไฟล์โค้ดทั้งหมดในโปรเจกต์เข้าไปใน container
COPY . .

# 7. คำสั่งที่จะรันเมื่อ container เริ่มทำงาน
CMD ["python", "mzsiri.py"]