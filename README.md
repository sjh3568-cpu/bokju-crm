# bokju-crm

## 전국 병원 공식명 마스터 등록

공공데이터/심평원 병원정보서비스 CSV를 받은 경우:

```powershell
.\.venv\Scripts\python.exe tools\import_hospital_master.py --csv C:\path\hospitals.csv
```

공공데이터 인증키가 있는 경우:

```powershell
$env:DATA_GO_KR_SERVICE_KEY="발급받은_서비스키"
.\.venv\Scripts\python.exe tools\import_hospital_master.py --api
```

기본값은 의원/약국을 제외하고 상급종합·종합병원·병원·요양병원·정신병원·치과병원·한방병원만 등록한다.
의원까지 포함하려면 `--include-clinics`를 추가한다.
