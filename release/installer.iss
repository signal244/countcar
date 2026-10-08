; Count Car 설치 프로그램 (Inno Setup 6). scripts/build_release.py 가 아래 값을 넘겨 만든다.
;   SrcDir  : 배포판 파일을 펼쳐 둔 폴더
;   AppVer  : 버전(날짜)
;   OutDir  : 결과 exe 를 둘 폴더
;   OutName : 결과 exe 이름(확장자 제외)
#ifndef SrcDir
  #error SrcDir 를 지정하세요
#endif
#ifndef AppVer
  #define AppVer "0"
#endif
#ifndef OutDir
  #define OutDir "."
#endif
#ifndef OutName
  #define OutName "CountCar_Setup"
#endif

[Setup]
; AppId 를 바꾸지 말 것: 같은 값이어야 새 버전이 기존 설치 위에 업데이트된다.
AppId={{8F2C6A31-4B7E-4C59-9D3A-6E1F0B27C4A5}
AppName=Count Car
AppVersion={#AppVer}
AppPublisher=Count Car
DefaultDirName={sd}\CountCar
DefaultGroupName=Count Car
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
; Inno Setup 6.5+ 는 기본으로 RedirectionGuard 를 켠다. 이것이 설치.bat(uv)에도 이어져
; Python 버전 폴더 링크(junction)를 만들지 못해 os error 448 로 설치가 실패했다.
; 관리자 권한 없이 사용자 폴더에만 설치하므로 끈다.
RedirectionGuard=no
; 32비트 모드면 설치.bat 이 SysWOW64 쪽에서 돌아 System32 의 nvidia-smi 를 못 찾고
; GPU 가 있어도 CPU 용 PyTorch 를 설치한다. 64비트 모드로 실행한다.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename={#OutName}
; 모델·영상은 이미 압축된 형식이라 빠른 압축으로 충분하다.
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
UninstallDisplayName=Count Car
ShowLanguageDialog=no

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Tasks]
Name: "desktopicon"; Description: "바탕화면에 Count Car 바로가기 만들기"

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Count Car\Count Car 실행"; Filename: "{app}\실행.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\Count Car\샘플로 성능 확인"; Filename: "{app}\샘플확인.bat"; WorkingDir: "{app}"
Name: "{autoprograms}\Count Car\설치 및 사용법"; Filename: "{win}\notepad.exe"; Parameters: """{app}\설치_및_사용법.md"""
Name: "{autoprograms}\Count Car\Count Car 제거"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Count Car"; Filename: "{app}\실행.bat"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\실행.bat"; Description: "지금 Count Car 실행"; WorkingDir: "{app}"; Flags: postinstall nowait skipifsilent unchecked

[UninstallDelete]
; 설치 때 내려받은 실행 환경. 사용자가 넣은 모델과 output(결과 DB·엑셀)은 남긴다.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\tools\python"
Type: filesandordirs; Name: "{app}\tools\uv-cache"

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  Params: String;
begin
  if CurStep = ssPostInstall then
  begin
    WizardForm.StatusLabel.Caption :=
      'Python 과 패키지를 설치하는 중입니다 (인터넷 필요, 10~20분). 검은 창을 닫지 마세요.';
    Params := '/c ""' + ExpandConstant('{app}') + '\설치.bat" /auto"';
    if (not Exec(ExpandConstant('{cmd}'), Params, ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode))
       or (ResultCode <> 0) then
      MsgBox('Python·패키지 설치가 끝나지 않았습니다.' + #13#10 +
             '인터넷 연결을 확인한 뒤, 설치 폴더의 설치.bat 을 더블클릭해 다시 설치하세요.',
             mbError, MB_OK);
  end;
end;
