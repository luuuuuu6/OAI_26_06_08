# Manual 3: AWS 클라우드 개발환경 구축 가이드

> **대상**: DCL 구성원 (Manual 1, 2 완료 후)
>
> **환경**: KIRO IDE (로컬) + EC2 Remote SSH + GitHub 연동
>
> **시리즈**: Manual 1 (Git 기초) → Manual 2 (팀 Integration) → **Manual 3 (AWS 클라우드)**

---

## 목차

1. [전체 구조 및 설계 원칙](#1-전체-구조-및-설계-원칙)
2. [AWS 계정 및 서비스 개요](#2-aws-계정-및-서비스-개요)
3. [인스턴스 선택 가이드](#3-인스턴스-선택-가이드)
   - [3.4 vCPU 한도 및 Service Quota 증가 요청](#34-vcpu-한도-및-service-quota-증가-요청)
4. [인스턴스 상태 관리 (Stop / Start / Terminate)](#4-인스턴스-상태-관리-stop--start--terminate)
5. [EC2 인스턴스 생성 (최초 1회)](#5-ec2-인스턴스-생성-최초-1회)
   - [5.3 키 페어 (.pem) 설정](#53-키-페어-pem-설정)
   - [5.4 스토리지 설정](#54-스토리지-설정)
   - [5.5 네트워크 및 보안 그룹 설정](#55-네트워크-및-보안-그룹-설정)
6. [KIRO 설치 및 AWS 로그인](#6-kiro-설치-및-aws-로그인)
7. [Remote SSH 설정](#7-remote-ssh-설정)
   - [7.2 일반 SSH 방식 (연구실 표준)](#72-일반-ssh-방식-연구실-표준)
   - [7.3 AWS 자격증명 설정](#73-aws-자격증명-설정-로컬-pc--1회)
   - [7.4 SSM 방식 (선택사항)](#74-ssm-방식-선택사항--고보안-환경)
8. [KIRO에서 EC2 연결하기](#8-kiro에서-ec2-연결하기)
   - [8.1 Open Remote - SSH 확장 설치](#81-remote-ssh-확장-설치-최초-1회)
   - [8.2 KIRO 계정 로그인 (필수)](#82-kiro-계정-로그인-ec2-연결-전-필수)
   - [8.3 편집박스 연결](#83-편집박스-연결)
9. [EC2 개발환경 구축 (Git + Docker)](#9-ec2-개발환경-구축-git--docker)
   - [9.1 GitHub SSH 키 설정](#91-github-ssh-키-설정)
   - [9.2 프로젝트 Clone](#92-프로젝트-clone)
   - [9.3 Docker 설치](#93-docker-설치)
   - [9.4 NVIDIA Container Toolkit (GPU 박스)](#94-nvidia-container-toolkit-gpu-박스만)
   - [9.5 컨테이너에서 git 사용](#95-컨테이너-안에서-git-사용하기)
   - [9.6 devcontainer.json 설정](#96-devcontainerjson-설정-kiro에서-권장)
10. [EC2 켜고 끄기](#10-ec2-켜고-끄기)
11. [KIRO MCP 설정 (AWS 연동 강화)](#11-kiro-mcp-설정-aws-연동-강화)
12. [일상 워크플로](#12-일상-워크플로)
13. [비용 관리 팁](#13-비용-관리-팁)
14. [자주 묻는 질문](#14-자주-묻는-질문)

---

## 1. 전체 구조 및 설계 원칙

### 1.1 구조 요약

```
[KIRO 로컬 PC]
   │
   ├─ Remote SSH ──→ c7i.2xlarge (편집박스, 상시 Stop/Start)
   │                      │ git push/pull
   │                      ▼
   │                 GitHub (main)       ← 단일 진실 소스
   │                      │ git pull
   │                      ▼
   └─ Remote SSH ──→ g6e.xlarge (GPU 검증박스, 필요시만 기동)
                           │ 결과 업로드
                           ▼
                           S3
                           │ KB 인덱싱
                           ▼
                        Bedrock (RAG)
```

### 1.2 설계 원칙

| 원칙 | 내용 |
|---|---|
| **코드는 GitHub 단방향 진실** | 모든 환경이 동일 리포에 push/pull |
| **편집과 실행 분리** | 편집은 EC2, 무거운 실행은 로컬 H100 서버 |
| **GPU 인스턴스는 필요시만** | Start → 실행 → Stop. 상시 켜두지 않음 |
| **접속은 SSM** | 포트 22 인터넷 노출 없이 EC2 접속 |
| **재현성은 ECR 이미지** | 로컬 H100 / 클라우드 EC2 / CI가 동일 도커 이미지 사용 |

### 1.3 역할 분담

| 환경 | 역할 | 비고 |
|---|---|---|
| **로컬 PC + KIRO** | 편집, 설계, 문서 작성 | Remote SSH로 EC2에 붙어서 작업 |
| **EC2 편집박스** (c7i) | 코드 편집, git 관리, 경량 테스트 | GPU 없음. Stop 자주. EBS만 유지 |
| **EC2 GPU 검증박스** (g6e) | CUDA 기능 확인, 에러 확인 | 필요시만 Start. 테스트 후 즉시 Stop |
| **로컬 H100 서버** | 실제 채널 시뮬레이션 실행, 성능 측정 | 본격 실행 환경. 지속 유지 |
| **GitHub** | 코드 단일 진실 소스, CI/CD | 모든 환경이 여기서 pull |
| **S3** | 데이터셋, 산출물, 회의록 저장 | 로컬 H100 ↔ 클라우드 전달 통로 |
| **Bedrock** | LLM 호출, 회의록 RAG | KIRO 에이전트가 문서 참조 시 사용 |

---

## 2. AWS 계정 및 서비스 개요

### 2.1 주요 서비스 한눈에 보기

| 서비스 | 역할 | 비고 |
|---|---|---|
| **EC2** | 가상 서버 (편집박스, GPU 검증박스) | 핵심 |
| **S3** | 파일 저장소 (데이터, 산출물, 회의록) | 로컬↔클라우드 전달 통로 |
| **ECR** | 도커 이미지 저장소 | 환경 재현성 |
| **Bedrock** | LLM 호출 및 RAG | KIRO 에이전트 연동 |
| **IAM Identity Center** | 팀 계정 통합 로그인 (SSO) | |
| **SSM Session Manager** | 포트 22 없이 EC2 접속 | 보안 필수 |
| **CloudWatch** | EC2 로그, 메트릭 모니터링 | |

### 2.2 EC2 vs SageMaker

우리 프로젝트(채널 시뮬레이터)는 **ML이 아니지만 GPU 연산량이 큰** 워크로드이므로 EC2가 적합하다.

| 항목 | SageMaker | EC2 |
|---|---|---|
| 목적 | ML 학습·실험 중심 | 범용 서버, 자유도 최대 |
| CUDA/드라이버 | AWS가 관리 (고정) | 직접 통제 가능 |
| 비용 | 같은 스펙에서 더 비쌈 | Spot/Savings Plan 자유롭게 활용 |
| 상주 편집 용도 | 비권장 | 적합 |
| 공유 메모리 제어 | 제한적 | 가능 |

> OAI(C)와 채널 시뮬레이터(Python)가 공유 메모리로 연결된 구조상 EC2에서 직접 제어가 필요하다.

---

## 3. 인스턴스 선택 가이드

### 3.1 프로젝트 기준 추천 구성

| 계층 | 인스턴스 | GPU | VRAM | 역할 | 비용(온디맨드) |
|---|---|---|---|---|---|
| **상주 편집박스** | `c7i.2xlarge` | 없음 | — | KIRO Remote, 코드 편집, git 관리 | ~$0.36/hr |
| **GPU 검증박스 (1순위)** | `g6e.xlarge` | L40S × 1 | 48GB | L40S 스케일링 테이블 검증, CUDA 에러 확인 | ~$2.2/hr |
| **GPU 검증박스 (대안)** | `g6.xlarge` | L4 × 1 | 24GB | g6e 가용 안 될 때 대안. N=1셀 수준 검증 | ~$1.0/hr |
| **H100 검증박스** | `p5.48xlarge` | H100 × 8 | 80GB × 8 | H100 스케일링 테이블 검증 (1장만 사용) | Spot ~$30~50/hr |

> ⚠️ **신규 계정 주의**: GPU 인스턴스(G, P 패밀리)는 신규 AWS 계정의 기본 vCPU 한도가 **0**이다. Service Quota 증가 요청 후 사용 가능하다. (§3.4 참조)

### 3.2 GPU 인스턴스 선택 근거

**채널 시뮬레이터의 메모리 구조 (회의록 기반)**

```
H100 80GB 기준:
  외부 버퍼 40GB  │  내부 연산 40GB   ← HBM 50:50 분할
  (IQ 버퍼, 링버퍼)  (채널 generation)

L40S 48GB 기준:
  외부 버퍼 24GB  │  내부 연산 24GB
```

- 연산량은 셀 수 N에 대해 **N² 스케일링** (셀 ×2 → 연산량 ×4)
- L40S 산출표와 H100 산출표 두 가지를 만들어야 하므로 두 인스턴스 유형 모두 필요
- EC2에서의 역할은 **"에러 확인"** — 성능 벤치마크는 로컬 H100 서버에서

**AWS에는 H100 단일 카드 인스턴스가 없다**

- `p5` 패밀리 최소 단위 = `p5.48xlarge` (H100 × 8장)
- H100 1장만 쓰려면: `CUDA_VISIBLE_DEVICES=0`으로 1장만 사용

```bash
# H100 1장만 사용하는 실행 예시
CUDA_VISIBLE_DEVICES=0 python run_channel_sim.py
```

### 3.3 인스턴스 패밀리 비교표

| 인스턴스 | GPU | VRAM | 위치 | 비고 |
|---|---|---|---|---|
| `g4dn.xlarge` | T4 | 16GB | 미국, 유럽, 서울 | 가장 저렴, 기능 확인용 |
| `g5.xlarge` | A10G | 24GB | 미국, 유럽 | 중간급, 간단한 CUDA 검증 |
| `g6.xlarge` | L4 | 24GB | 미국 | g6e 없을 때 대안. N=1셀 수준 |
| `g6e.xlarge` | L40S | 48GB | 미국 | **권장**: L40S 산출표 검증 |
| `p5.48xlarge` | H100 × 8 | 80GB × 8 | 미국 | H100 산출표 검증, Spot만 사용 |

> ⚠️ G/P 패밀리는 서울 리전(ap-northeast-2) 가용성이 낮다. 미국 리전(us-east-1, us-west-2) 권장.

**g6.xlarge (L4 24GB) VRAM 제약**

```
g6.xlarge: L4 24GB
  → 외부 버퍼 12GB + 내부 연산 12GB (50:50)
  → N=1셀, 4T4R, 8UE까지 테스트 가능
  → N=2셀 이상은 VRAM 부족 가능성 있음
```

g6e(L40S 48GB)가 가용하면 g6e를 우선 사용한다.

### 3.4 vCPU 한도 및 Service Quota 증가 요청

**신규 AWS 계정의 GPU 인스턴스 기본 한도**

| 패밀리 | 해당 인스턴스 | 신규 계정 기본 한도 |
|---|---|---|
| **Standard** (A, C, D, H, I, M, R, T, Z) | `c7i.2xlarge`, `t3.medium` 등 | **32 vCPU** — 즉시 사용 가능 |
| **G and VT** | `g6.xlarge`, `g6e.xlarge`, `g5`, `g4dn` | **0 vCPU** — 요청 필요 |
| **P** | `p5.48xlarge` 등 | **0 vCPU** — 요청 필요 |

→ GPU 인스턴스 시작 시 "vCPU limit of 0" 오류가 뜨면 한도 증가 요청이 필요하다.

**Service Quota 증가 요청 방법**

```
Step 1. AWS 콘솔 검색창 → "Service Quotas" 검색 → 클릭
Step 2. 왼쪽 메뉴: AWS 서비스 → "Amazon EC2" 클릭
Step 3. 검색창: "Running On-Demand G" 입력
Step 4. "Running On-Demand G and VT instances" 클릭
Step 5. 오른쪽 상단 "할당량 증가 요청" 버튼 클릭
Step 6. 요청 값: 4 입력  (g6.xlarge 1대 = 4 vCPU)
Step 7. "요청" 클릭
```

- 승인 소요 시간: 수 시간 ~ 1~2일
- 승인 결과: 가입 이메일로 발송, Service Quotas → 요청 기록 탭에서도 확인 가능

**한도 승인 대기 중 권장 행동**

```
지금 바로:  c7i.2xlarge 편집박스 생성 → SSH/GitHub 연동 세팅
병행:       G and VT 한도 증가 요청
승인 후:    g6.xlarge 또는 g6e.xlarge 추가 생성 → GPU 검증 시작
```

---

## 4. 인스턴스 상태 관리 (Stop / Start / Terminate)

### 4.1 먼저 알아야 할 것 — EBS란?

**EBS(Elastic Block Store)** 는 EC2 인스턴스에 붙은 **하드디스크**입니다. EC2를 "컴퓨터 본체(CPU/메모리)"라고 보면, EBS는 "내장 SSD"에 해당합니다.

| 항목 | EC2 (컴퓨팅) | EBS (스토리지) |
|---|---|---|
| **역할** | CPU, 메모리, GPU | 디스크 (코드, 데이터, OS) |
| **과금 기준** | Running 시간 | 용량 × 보관 기간 |
| **Stop 시** | ❌ 과금 중지 | ✅ 과금 지속 |
| **Terminate 시** | ❌ 과금 중지 | ❌ 함께 삭제 (기본) |

> 즉, 인스턴스를 Stop해도 **EBS 비용은 계속 나갑니다.** 대신 아주 저렴합니다 — 30GB gp3 기준 약 **$3/월**, 50GB 기준 약 **$4/월**.

이렇게 EC2와 EBS가 분리되어 있어서:
- Stop → 컴퓨팅만 끄고 디스크 내용(설치한 패키지, 코드, git 기록)은 **그대로 보존**
- Start → 껐던 그 상태 그대로 부팅

### 4.2 세 가지 상태 구분

```
Running (실행 중)  ──→  Stopped (중지)  ──→  Terminated (종료/삭제)
 컴퓨팅 + EBS 과금       EBS만 과금              모두 삭제, 비용 없음
                        (~$3/월 per 30GB)
```

| 상태 | 컴퓨팅 요금 | EBS 요금 | 데이터 보존 | 재사용 |
|---|---|---|---|---|
| **Running** | ✅ 과금 | ✅ 과금 | ✅ 유지 | ✅ 가능 |
| **Stopped** | ❌ 없음 | ✅ 과금 | ✅ 유지 | ✅ 가능 |
| **Terminated** | ❌ 없음 | ❌ 없음 | ❌ 삭제 | ❌ 불가 |

### 4.3 권장 운영 방식

**편집박스 (c7i.2xlarge)**

```
만들 때:    한 번만 생성 (Terminate하지 않음)
작업할 때:  Start
작업 끝나면: Stop → EBS 비용만 발생 (~$3/월)
종료 시:    프로젝트 완전 종료 시에만 Terminate
```

**GPU 검증박스 (g6e.xlarge / p5.48xlarge)**

```
만들 때:    한 번만 생성 (또는 Launch Template 저장)
검증할 때:  Start → 실행 → Stop
비용:       켜진 시간만큼만 청구
```

> **Stop ≠ Terminate**. 매번 지울 필요 없이 Stop해두면 EBS 비용만 내면서 설정과 데이터를 보존할 수 있다.

### 4.4 한 달 비용 예시

| 사용 패턴 | 예상 비용 |
|---|---|
| c7i.2xlarge 하루 4시간, 20일 사용 | $0.36 × 80hr = **$29** |
| c7i.2xlarge Stop 상태 유지 (30GB EBS) | **~$3/월** |
| g6e.xlarge 주 1회 2시간, 4회 사용 | $2.2 × 8hr = **$18** |
| p5.48xlarge Spot 1회 1시간 사용 | ~$30~50 × 1hr = **$30~50** |

---

## 5. EC2 인스턴스 생성 (최초 1회)

AWS 콘솔 웹에서 생성한다. KIRO는 이후 §6에서 설치한다.

### 5.1 편집박스 생성 (c7i.2xlarge)

```
AMI:              Ubuntu 22.04 LTS
Instance type:    c7i.2xlarge
Storage:          50GiB gp3  (기본 8GiB gp2에서 변경 필요)
Key pair:         새로 생성 후 .pem 저장 (Remote SSH에 사용)
Security Group:   인바운드 없음, 아웃바운드 전체 허용
IAM Instance Profile: SSM 접속 권한 포함된 Role 부착
```

### 5.2 GPU 검증박스 생성 (g6.xlarge / g6e.xlarge)

```
AMI:              AWS Deep Learning Base AMI (Ubuntu 22.04) — CUDA 사전 설치
Instance type:    g6e.xlarge (L40S 48GB) 또는 g6.xlarge (L4 24GB)
Storage:          50GiB gp3  (기본 8GiB gp2에서 변경 필요)
Key pair:         편집박스와 동일 키페어 사용 가능
Security Group:   편집박스와 동일
IAM Instance Profile: 편집박스와 동일
```

> AWS Deep Learning Base AMI를 사용하면 CUDA, NVIDIA 드라이버, Docker가 사전 설치되어 있어 별도 설정이 불필요하다.

### 5.3 키 페어 (.pem) 설정

키 페어는 EC2 접속 시 **신원 확인에 쓰이는 개인 키**다. 생성 시 자동 다운로드되며 재발급이 불가하므로 안전하게 보관한다.

**저장 위치 및 권한 설정 (로컬 PC에서)**

```bash
# ~/.ssh/ 폴더에 이동 (관례적 위치)
mv ~/Downloads/your-key.pem ~/.ssh/your-key.pem

# 권한 설정 필수 — 없으면 SSH 접속 거부됨
chmod 400 ~/.ssh/your-key.pem
```

- 저장 위치는 어디든 가능하지만 `~/.ssh/`가 관례
- `~/.ssh/config`의 `IdentityFile` 경로에 지정하면 자동 사용
- ⚠️ `.pem` 파일은 **절대 GitHub에 올리면 안 됨** (`.gitignore`에 `*.pem` 추가 권장)

### 5.4 스토리지 설정

콘솔 기본값(8GiB gp2)은 부족하다. 아래와 같이 변경한다.

| 항목 | 기본값 | 권장값 | 이유 |
|---|---|---|---|
| 용량 | 8 GiB | **50 GiB** | OS + CUDA + 코드 + 테스트 데이터 |
| 타입 | gp2 | **gp3** | 더 저렴하고 기본 IOPS(3000)가 더 높음 |

파일 시스템 옵션(S3 파일, EFS, FSx)은 **선택하지 않아도 된다**. S3 연동은 `aws s3 sync` 명령어로 필요할 때만 사용한다.

### 5.5 네트워크 및 보안 그룹 설정

SSM 방식으로 접속하므로 인바운드 SSH 규칙이 필요 없다.

**접속 방식 비교**

```
일반 SSH:  외부 ──포트 22──→ EC2   (인바운드 0.0.0.0/0 열어야 함, 보안 취약)
SSM:       EC2 ──아웃바운드──→ AWS SSM 엔드포인트
              KIRO가 SSM 터널을 통해 접속 (인바운드 불필요)
```

**권장 보안 그룹 설정**

```
인바운드 규칙:  없음  ← "SSH 트래픽 허용" 체크 해제
아웃바운드 규칙: 모든 트래픽 허용  (기본값 그대로)
```

아웃바운드 허용이 있어야 EC2가 SSM 엔드포인트, S3, ECR에 접근할 수 있다.

### 5.6 IAM Instance Profile 권한 설정

EC2가 SSM, S3, ECR에 접근할 수 있도록 IAM Role을 생성하고 인스턴스에 부착한다.

```
IAM 콘솔 → Roles → Create role
Trusted entity: EC2
Permissions:
  - AmazonSSMManagedInstanceCore   ← SSM 접속 필수
  - AmazonS3ReadOnlyAccess         ← S3 데이터셋 다운로드
  - AmazonEC2ContainerRegistryFullAccess ← ECR 이미지 pull
```

---

## 6. KIRO 설치 및 AWS 로그인

### 6.1 KIRO 설치

```
다운로드: https://kiro.dev
```

- VS Code 포크 기반 — 기존 확장, 단축키 대부분 호환
- Cursor의 Rules/Hooks/MCP와 동일한 개념을 Steering Files/Hooks/MCP로 지원
- AWS Bedrock, IAM Identity Center와 **네이티브 연동** (설정 없이 바로 사용)

### 6.2 AWS 로그인

KIRO 실행 → 좌측 하단 **"Sign in"** 클릭

| 로그인 방식 | 사용 상황 |
|---|---|
| **AWS Builder ID** | 개인 계정으로 처음 시작할 때 |
| **IAM Identity Center (SSO)** | 팀 공용 AWS 계정이 있을 때 (권장) |

IAM Identity Center 사용 시 팀 전체가 같은 계정으로 관리되고, 로컬 PC의 `aws configure sso`와 연동된다.

```bash
# 터미널에서 SSO 로그인 (AWS CLI 설치 필요)
aws configure sso
aws sso login --profile default
```

---

## 7. Remote SSH 설정

EC2에 SSH로 접속하는 방법은 두 가지다. 연구실에서는 **일반 SSH 방식**을 주로 사용한다.

| 방식 | 보안 그룹 | IP 문제 | IAM Role | 난이도 |
|---|---|---|---|---|
| **일반 SSH** ← 연구실 표준 | 포트 22 오픈 | Stop/Start 시 IP 바뀜 | 불필요 | 쉬움 |
| **SSM** | 인바운드 없음 | IP 무관 (Instance ID 사용) | 필요 | 복잡 |

### 7.1 AWS CLI + Session Manager 플러그인 설치 (로컬 PC)

이미 설치되어 있으면 확인 후 생략 가능하다.

```bash
aws --version                      # AWS CLI 확인
session-manager-plugin --version   # SSM 플러그인 확인
```

**macOS**

```bash
# Homebrew 사용 (권장)
brew install awscli
brew install --cask session-manager-plugin

# 또는 pkg 파일로 설치
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "AWSCLIV2.pkg"
sudo installer -pkg AWSCLIV2.pkg -target /

curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/mac/session-manager-plugin.pkg" \
  -o "session-manager-plugin.pkg"
sudo installer -pkg session-manager-plugin.pkg -target /
```

**Windows (PowerShell — 관리자 권한으로 실행)**

```powershell
# AWS CLI (winget 사용)
winget install Amazon.AWSCLI

# 또는 msi 설치
Invoke-WebRequest -Uri "https://awscli.amazonaws.com/AWSCLIV2.msi" -OutFile "AWSCLIV2.msi"
Start-Process msiexec.exe -ArgumentList "/i AWSCLIV2.msi /quiet" -Wait

# Session Manager 플러그인
Invoke-WebRequest `
  -Uri "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/windows/SessionManagerPluginSetup.exe" `
  -OutFile "SessionManagerPluginSetup.exe"
Start-Process SessionManagerPluginSetup.exe -ArgumentList "/quiet" -Wait
```

설치 후 PowerShell 재시작 후 확인:

```powershell
aws --version
session-manager-plugin --version
```

**Linux (Ubuntu)**

```bash
# AWS CLI
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip && sudo ./aws/install

# Session Manager 플러그인
curl "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o "session-manager-plugin.deb"
sudo dpkg -i session-manager-plugin.deb
```

### 7.2 일반 SSH 방식 (연구실 표준)

포트 22를 열고 퍼블릭 IP + .pem 키로 직접 접속한다.

**SSH config 설정 (`~/.ssh/config`)**

```
# 편집박스
Host ec2-edit
    HostName 13.238.159.4        ← EC2 퍼블릭 IPv4 주소
    User ubuntu
    IdentityFile ~/.ssh/your-key.pem

# GPU 검증박스
Host ec2-gpu
    HostName 1.2.3.4             ← GPU 박스 퍼블릭 IPv4 주소
    User ubuntu
    IdentityFile ~/.ssh/your-key.pem
```

> 퍼블릭 IP 확인: EC2 콘솔 → 인스턴스 선택 → **퍼블릭 IPv4 주소**

**접속**

```bash
ssh ec2-edit
```

**⚠️ Stop/Start 후 IP가 바뀌면**

인스턴스를 Stop 후 Start하면 퍼블릭 IP가 새로 할당된다. 새 IP를 확인해서 SSH config의 `HostName`을 업데이트해야 한다.

```bash
# 새 퍼블릭 IP 확인
aws ec2 describe-instances \
  --instance-ids i-xxxxxxxxxxxxxxxxx \
  --query 'Reservations[0].Instances[0].PublicIpAddress' \
  --output text
```

---

### 7.3 AWS 자격증명 설정 (로컬 PC — 1회)

`aws` CLI 명령어 사용 시 필요하다. **연구실 구성원은 각자 액세스 키와 비밀 액세스 키를 발급받아 사용한다.**

```bash
aws configure
```

```
AWS Access Key ID [None]: AKIA...           ← 내 액세스 키
AWS Secret Access Key [None]: xxxxxxxx     ← 내 비밀 액세스 키
Default region name [None]: ap-northeast-2  ← 인스턴스가 있는 리전
Default output format [None]: json
```

> 리전 확인: EC2 콘솔 오른쪽 상단 드롭다운. 서울 = `ap-northeast-2`, 시드니 = `ap-southeast-2`, 버지니아 = `us-east-1`

입력한 키는 `~/.aws/credentials`에 저장되어 이후 `aws` 명령어 실행 시 자동으로 사용된다.

```bash
# 설정 확인
aws sts get-caller-identity
```

---

### 7.4 SSM 방식 (선택사항 — 고보안 환경)

IP 변동과 무관하게 Instance ID로 접속한다. IAM Role 부착이 선행되어야 한다.

**SSH config 설정 (`~/.ssh/config`)**

```
# 편집박스
Host ec2-edit
    HostName i-xxxxxxxxxxxxxxxxx
    User ubuntu
    ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'
    IdentityFile ~/.ssh/your-key.pem

# GPU 검증박스
Host ec2-gpu
    HostName i-yyyyyyyyyyyyyyyyy
    User ubuntu
    ProxyCommand aws ssm start-session --target %h --document-name AWS-StartSSHSession --parameters 'portNumber=%p'
    IdentityFile ~/.ssh/your-key.pem
```

> ⚠️ `ProxyCommand`는 반드시 **한 줄**로 써야 한다. 백슬래시(`\`)로 줄바꿈하면 SSH config에서 오류 발생.

> Instance ID는 EC2 콘솔 → 인스턴스 선택 → 상세 정보에서 확인 (`i-`로 시작하는 문자열)

SSM 방식 사전 조건:
- EC2에 IAM Role 부착 (`AmazonSSMManagedInstanceCore` 정책 포함)
- 보안 그룹 인바운드: 없음, 아웃바운드: 전체 허용

### 7.5 접속 확인

```bash
ssh ec2-edit   # 편집박스 접속 테스트
ssh ec2-gpu    # GPU 검증박스 접속 테스트 (Running 상태일 때)
```

---

## 8. KIRO에서 EC2 연결하기

KIRO는 `Open Remote - SSH` 확장을 통해 EC2에 접속한다. **확장 설치 + KIRO 계정 로그인** 두 가지가 모두 필요하다.

### 8.1 Remote-SSH 확장 설치 (최초 1회)

```
1. Ctrl+Shift+X  (확장 패널 열기)
2. 검색창에 "Remote - SSH" 입력
3. jeanp413의 "Open Remote - SSH" (다운로드 292K) → Install 클릭
4. 설치 완료 후 KIRO 재시작 (필요 시)
```

설치 후에는 좌측 하단에 리모콘 모양 아이콘이 생긴다.

### 8.2 KIRO 계정 로그인 (EC2 연결 전 필수)

KIRO로 EC2 원격 접속을 하려면 **KIRO 계정 세션이 유효해야** 한다. 세션이 만료되면 SSH 연결이 풀린다.

```
1. KIRO 실행 후 좌측 하단 계정 아이콘 클릭
   또는 Ctrl+Shift+P → "KIRO: Sign In"
2. "Sign in via IAM Identity Center" 선택
3. Organization 입력
4. MFA 인증 완료
```

> 로그인이 풀려 있으면 EC2 연결 시도 자체가 실패한다. 연결이 안 될 때 가장 먼저 확인할 것.

### 8.3 편집박스 연결

**방법 A — 리모콘 아이콘**

```
좌측 하단 리모콘 아이콘 클릭 → "Connect to Host..." → ec2-edit 입력 또는 선택
```

**방법 B — 명령어 팔레트**

```
Ctrl+Shift+P → "Remote-SSH: Connect to Host" 입력 → ec2-edit 선택
```

> 목록이 비어 있으면 호스트명(`ec2-edit`)을 직접 입력하면 된다.

연결 후에는 **KIRO 에이전트, Specs, Hooks 전부 EC2 위에서 동작**한다.

- 파일 탐색기: EC2의 파일 시스템
- KIRO 터미널: EC2의 bash
- git 명령어: EC2에서 실행

### 8.4 GPU 검증박스 연결

GPU 박스가 **Running 상태일 때만** 연결 가능하다.

```
Ctrl+Shift+P → "Remote-SSH: Connect to Host" → ec2-gpu 선택
```

작업이 끝나면 GPU 박스 터미널에서 종료:

```bash
sudo shutdown -h now
```

### 8.5 여러 EC2를 동시에 연결

KIRO에서 **새 창(New Window)**으로 열면 편집박스와 GPU 검증박스를 동시에 연결할 수 있다.

```
창 1: KIRO → ec2-edit (코드 편집)
창 2: KIRO → ec2-gpu  (실행 결과 확인)
```

---

## 9. EC2 개발환경 구축 (Git + Docker)

EC2에 처음 접속한 뒤, 실제 코드를 내려받고 실행 환경을 구축하는 단계다. **인스턴스마다 최초 1회** 필요하다.

### 9.1 GitHub SSH 키 설정

EC2는 연구실 기존 서버와 마찬가지로 **별개의 컴퓨터**다. GitHub에 접근하려면 EC2용 SSH 키를 새로 생성해 GitHub에 등록해야 한다.

**1. EC2에서 SSH 키 생성**

```bash
ssh-keygen -t ed25519 -C "ec2-edit"
# 질문 세 번 전부 Enter
```

**2. 공개키 확인 및 복사**

```bash
cat ~/.ssh/id_ed25519.pub
```

출력된 공개키 전체(`ssh-ed25519 AAAA...`) 복사.

**3. GitHub에 등록**

```
GitHub → Settings → SSH and GPG keys → New SSH key
  Title: EC2-edit (인스턴스별 구분용 이름)
  Key:   복사한 공개키 붙여넣기
  → Add SSH key
```

> 인스턴스가 여러 개라면 **각각 별도로 키를 생성해 GitHub에 등록**한다. 보안상 권장되며, 어느 한 대의 키만 개별적으로 GitHub에서 제거할 수 있다.

**4. Git 사용자 정보 설정 (최초 1회)**

```bash
git config --global user.name "Your Name"
git config --global user.email "your@email.com"
```

### 9.2 프로젝트 Clone

```bash
cd ~
git clone git@github.com:YOUR_ORG/DevChannelProxyJIN.git
cd DevChannelProxyJIN
```

> `git clone`은 **최초 1회**만 실행한다. 이후 변경사항을 받을 때는 `git pull`을 사용.

| 명령 | 언제 쓰나 | 동작 |
|---|---|---|
| `git clone` | 처음 1회 | 저장소를 통째로 복제 (폴더 + 히스토리) |
| `git pull` | 이후 매번 | 변경분만 받아서 병합 |

### 9.3 Docker 설치

기본 Ubuntu AMI에는 Docker가 없다. **각 인스턴스마다 1회** 설치 필요.

```bash
sudo apt update
sudo apt install -y docker.io
sudo usermod -aG docker $USER
newgrp docker

docker --version
```

> `usermod`로 그룹에 추가한 후 재로그인하거나 `newgrp docker`를 실행해야 `sudo` 없이 docker 명령을 사용할 수 있다.

### 9.4 NVIDIA Container Toolkit (GPU 박스만)

GPU 박스에서 CUDA 컨테이너를 돌리려면 추가 설치가 필요하다. **편집박스에서는 불필요.**

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sudo sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list

sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo systemctl restart docker
```

**테스트**

```bash
docker run --gpus all --rm nvidia/cuda:12.2.0-devel-ubuntu22.04 nvidia-smi
```

GPU 정보가 정상 출력되면 설치 완료.

### 9.5 컨테이너 안에서 git 사용하기

컨테이너 안에서도 `git pull/push`가 필요하다면 **호스트의 SSH 키와 gitconfig를 read-only로 마운트**한다. 이미지에 키를 COPY하면 절대 안 된다(유출 위험).

**docker run 예시**

```bash
docker run --gpus all -it --rm \
  -v $(pwd):/workspace \
  -v ~/.ssh:/root/.ssh:ro \
  -v ~/.gitconfig:/root/.gitconfig:ro \
  -w /workspace \
  <image-name> bash
```

| 옵션 | 역할 |
|---|---|
| `-v $(pwd):/workspace` | 현재 프로젝트 폴더를 컨테이너에 공유 |
| `-v ~/.ssh:/root/.ssh:ro` | SSH 키 read-only 공유 |
| `-v ~/.gitconfig:/root/.gitconfig:ro` | git user 정보 공유 |
| `:ro` | read-only. 컨테이너에서 키 수정/삭제 방지 |

이렇게 하면 컨테이너 안에서도 `git pull/push`가 호스트 키로 인증된다. 호스트와 컨테이너가 **같은 폴더, 같은 키**를 보므로 어느 쪽에서 commit하든 동기화된다.

> ❌ **절대 금지**: Dockerfile에 `COPY id_ed25519 ...`. 이미지가 ECR에 올라가는 순간 개인키가 팀 전체에 노출된다.

### 9.6 devcontainer.json 설정 (KIRO에서 권장)

프로젝트에 `devcontainer.json`이 있으면 KIRO의 "Reopen in Container" 한 번으로 컨테이너 작업이 가능하다. §9.5의 마운트를 자동화하려면 다음 항목을 추가한다.

```json
{
  "mounts": [
    "source=${localEnv:HOME}/.ssh,target=/root/.ssh,type=bind,readonly",
    "source=${localEnv:HOME}/.gitconfig,target=/root/.gitconfig,type=bind,readonly"
  ],
  "runArgs": ["--gpus", "all"]
}
```

| 항목 | 역할 |
|---|---|
| `mounts` | 호스트의 SSH 키와 gitconfig를 컨테이너에 자동 마운트 |
| `runArgs: ["--gpus", "all"]` | GPU 컨테이너라면 GPU 접근 권한 부여 (편집박스에서는 제거) |

이제 매번 긴 `docker run` 명령 없이 KIRO에서 컨테이너 작업을 바로 시작할 수 있다.

### 9.7 설치 자동화 (선택사항)

인스턴스를 자주 만들고 삭제한다면 위 설치 과정을 자동화할 수 있다.

**방법 1: User Data로 부팅 시 자동 실행**

EC2 생성 시 "고급 세부 정보 → 사용자 데이터"에 입력하면 첫 부팅 때 자동 실행된다.

```bash
#!/bin/bash
apt update
apt install -y docker.io
usermod -aG docker ubuntu
```

**방법 2: AMI로 스냅샷 저장**

이미 설정이 끝난 인스턴스를 이미지로 저장해두면, 이후 인스턴스 생성 시 해당 AMI에서 바로 시작할 수 있다 (Docker/GitHub 키 등 모두 포함).

```
EC2 콘솔 → 인스턴스 우클릭 → "이미지 생성" → 이름 지정
```

새 인스턴스 생성 시 AMI 선택 → "내 AMI" 탭에서 선택.

---

## 10. EC2 켜고 끄기

### 9.1 세 가지 작업 구분 (가장 중요)

| 작업 | 의미 | 비용 | 데이터 | 언제 쓰나 |
|---|---|---|---|---|
| **Start (켜기)** | Stopped → Running | ✅ 과금 시작 | ✅ 유지 | 작업 시작할 때 |
| **Stop (끄기)** | Running → Stopped | ❌ 컴퓨팅 과금 정지 (EBS만) | ✅ 유지 | 작업 끝나고 잠시 쉴 때 |
| **Terminate (완전종료)** | 인스턴스 완전 삭제 | ❌ 모든 과금 정지 | ❌ **삭제됨** | 프로젝트 완전 종료 시 |

> ⚠️ **Terminate는 되돌릴 수 없습니다.** 평소에는 **Stop만** 사용하고, Terminate는 인스턴스를 더 이상 쓰지 않을 때만 실행하세요.

### 9.2 방법 비교

| 방법 | Start | Stop | Terminate | 특징 |
|---|---|---|---|---|
| **AWS 콘솔 (웹)** | ✅ | ✅ | ✅ | 가장 직관적, 상태 확인 동시 가능 |
| **AWS CLI** | ✅ | ✅ | ✅ | 빠름, 스크립트화 가능 (권장) |
| **KIRO 에이전트 + MCP** | ✅ | ✅ | ✅ | 자연어로 제어 |
| **EC2 내부 shutdown** | ❌ | ✅ (Stop만) | ❌ | SSH 접속 상태에서 바로 종료 |

---

### 9.3 AWS 콘솔 (웹)에서 제어

```
1. https://console.aws.amazon.com/ec2/ 접속
2. 왼쪽 메뉴 → "인스턴스"
3. 제어할 인스턴스 체크박스 선택
4. 우측 상단 "인스턴스 상태" 드롭다운에서 선택:
   - 인스턴스 시작 (Start)       → Stopped 상태에서 클릭 가능
   - 인스턴스 중지 (Stop)        → Running 상태에서 클릭 가능
   - 인스턴스 종료 (Terminate)  → ⚠️ 인스턴스 영구 삭제, 되돌릴 수 없음
```

> 현재 상태(Running/Stopped), 퍼블릭 IP, CPU/메모리 사용량을 한눈에 볼 수 있습니다.

---

### 9.4 AWS CLI로 제어

```bash
# 인스턴스 ID 및 상태 확인
aws ec2 describe-instances \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,Tags[?Key==`Name`].Value]'
```

**Start (켜기)**
```bash
aws ec2 start-instances --instance-ids i-xxxxxxxxxxxxxxxxx

aws ec2 wait instance-running --instance-ids i-xxxxxxxxxxxxxxxxx
```

**Stop (끄기) — 평소 사용**
```bash
aws ec2 stop-instances --instance-ids i-xxxxxxxxxxxxxxxxx
```

**Terminate (완전종료) — 주의: 되돌릴 수 없음**
```bash
aws ec2 terminate-instances --instance-ids i-xxxxxxxxxxxxxxxxx
```

> ⚠️ `terminate-instances`는 인스턴스와 연결된 EBS 볼륨(디스크)까지 삭제됩니다. 실행 전 두 번 확인하세요.

---

### 9.5 KIRO 에이전트로 제어 (MCP 설정 후)

§11의 KIRO MCP 설정을 완료하면, KIRO 채팅창에서 자연어로 제어할 수 있습니다.

```
"GPU 박스 켜줘"                          → Start
"편집박스 상태 확인해줘"                  → describe
"작업 끝났으니 GPU 박스 꺼줘"             → Stop
"이 인스턴스 완전히 삭제해줘"             → Terminate (확인 후 실행)
```

KIRO가 내부적으로 AWS MCP를 호출해 해당 명령을 실행합니다. **Terminate는 반드시 확인 단계를 거치도록 요청하세요.**

---

### 9.6 EC2 내부에서 shutdown (Stop만 가능)

GPU 박스에서 작업을 마치고 바로 **Stop**할 때 가장 간단합니다.

```bash
sudo shutdown -h now
```

SSH 연결은 끊기고, 인스턴스는 자동으로 **Stopped** 상태가 됩니다. (Terminate는 불가 — 인스턴스 외부에서 실행해야 함)

---

### 9.7 alias 등록 (편의성)

편집박스의 `~/.bashrc`에 추가:

```bash
# GPU 박스 제어 alias
GPU_ID="i-yyyyyyyyyyyyyyyyy"    # GPU 박스 Instance ID

alias gpu-on="aws ec2 start-instances --instance-ids $GPU_ID && \
              aws ec2 wait instance-running --instance-ids $GPU_ID && \
              echo 'GPU 박스 준비됨'"

alias gpu-off="aws ec2 stop-instances --instance-ids $GPU_ID && \
               echo 'GPU 박스 종료'"

alias gpu-status="aws ec2 describe-instances --instance-ids $GPU_ID \
  --query 'Reservations[0].Instances[0].State.Name' --output text"
```

```bash
source ~/.bashrc

gpu-on      # GPU 박스 기동
gpu-status  # 상태 확인
gpu-off     # GPU 박스 종료
```

### 9.8 검증 자동화 스크립트

편집박스에서 코드 push 후 GPU 박스에서 자동으로 pull & 실행하는 스크립트:

```bash
#!/bin/bash
# verify_on_gpu.sh

GPU_ID="i-yyyyyyyyyyyyyyyyy"

# GPU 박스 기동
echo "GPU 박스 기동 중..."
aws ec2 start-instances --instance-ids $GPU_ID
aws ec2 wait instance-running --instance-ids $GPU_ID

# SSH로 GPU 박스에서 실행
ssh ec2-gpu "cd ~/OAI-Channel && git pull && \
             CUDA_VISIBLE_DEVICES=0 python run_channel_sim.py && \
             aws s3 sync results/ s3://dcl-artifacts/channel-sim/ && \
             sudo shutdown -h now"

echo "검증 완료. GPU 박스 종료됨."
```

---

## 11. KIRO MCP 설정 (AWS 연동 강화)

KIRO 에이전트가 AWS 리소스를 직접 제어할 수 있도록 MCP 서버를 등록한다.

### 10.1 MCP 설정 파일 위치

```
~/.kiro/settings.json  또는
프로젝트/.kiro/settings.json
```

### 10.2 AWS MCP 설정 추가

```json
{
  "mcpServers": {
    "aws-core": {
      "command": "uvx",
      "args": ["awslabs.core-mcp-server@latest"],
      "env": {
        "AWS_REGION": "ap-northeast-2",
        "AWS_PROFILE": "default"
      }
    }
  }
}
```

### 10.3 MCP 설정 후 사용 가능한 KIRO 에이전트 명령

```
"GPU 검증 인스턴스 시작하고 상태 확인해줘"
"S3 dcl-artifacts 버킷에서 최신 결과 파일 목록 보여줘"
"CloudWatch에서 ec2-gpu의 최근 로그 확인해줘"
```

### 10.4 참고: AWS MCP 서버 목록

| MCP 서버 | 기능 |
|---|---|
| `awslabs.core-mcp-server` | EC2, S3, 기본 AWS 서비스 |
| `awslabs.cloudwatch-mcp-server` | 로그, 메트릭 조회 |
| `awslabs.bedrock-kb-retrieval-mcp-server` | Bedrock Knowledge Base 질의 |

전체 목록: [AWS Labs MCP Servers (GitHub)](https://github.com/awslabs/mcp)

---

## 12. 일상 워크플로

### 11.1 기본 개발 루프

```bash
# Step 1. KIRO → ec2-edit 연결 (편집박스)
# Ctrl+Shift+P → Connect to Host → ec2-edit

# Step 2. 최신 코드 받기
git pull org main

# Step 3. 코드 수정 (KIRO 에디터에서)

# Step 4. 커밋 & 푸시
git add .
git commit -m "feat: 설명"
git push org jin/작업이름

# Step 5. GPU 검증 필요 시
gpu-on                    # GPU 박스 기동
# 별도 KIRO 창: Connect to Host → ec2-gpu
git pull org jin/작업이름  # GPU 박스에서 최신 코드 받기
CUDA_VISIBLE_DEVICES=0 python run_channel_sim.py
gpu-off                   # 검증 완료 후 GPU 박스 종료

# Step 6. 결과 S3 업로드 (GPU 박스에서)
aws s3 sync results/ s3://dcl-artifacts/channel-sim/$(date +%Y%m%d)/
```

### 11.2 코드와 실행 환경의 흐름

```
[편집박스 - 코드 편집]
       │ git push
       ▼
  GitHub (main)
       │ git pull  ← 두 환경이 GitHub를 통해 코드 공유
       ▼
[GPU 검증박스 - 기능 확인]    [로컬 H100 서버 - 본격 실행]
       │                              │
       └──── S3에 결과 업로드 ─────────┘
```

- **두 인스턴스를 동시에 SSH 세션으로 연결할 필요 없음**
- GitHub이 중간 다리 역할 → 편집박스에서 push하면 GPU 박스에서 pull

### 11.3 로컬 H100 서버와의 연동

```bash
# 로컬 H100 서버에서 (기존 방식 그대로)
git pull org main                                    # GitHub에서 최신 코드
docker pull <ECR주소>/oai-channel:latest             # 공통 이미지 pull
aws s3 sync s3://dcl-datasets/ray-tracing/ data/    # 데이터 다운로드
python run_channel_sim.py                            # 본격 실행
aws s3 sync results/ s3://dcl-artifacts/...         # 결과 업로드
```

### 11.4 KIRO Specs 활용 (문서 기반 개발)

스케일링 테이블 측정, 파이프라인 설계 등을 Specs로 관리하면 KIRO 에이전트가 문서를 참조해서 코드를 작성한다.

```
OAI-Channel/
└── .kiro/
    └── specs/
        └── channel-sim/
            ├── requirements.md   ← 측정 목표 (N셀×K단말 조건)
            ├── design.md         ← HBM 50:50 분할 설계, 파이프라인 구조
            └── tasks.md          ← 검증 체크리스트
```

이 specs 파일들을 S3 `rag-sources/` 버킷에 동기화하면, Bedrock Knowledge Base가 인덱싱하여 KIRO 에이전트가 RAG로 참조한다.

```bash
# specs → S3 동기화 (편집박스에서)
aws s3 sync .kiro/specs/ s3://dcl-rag-sources/specs/
```

---

## 13. 비용 관리 팁

| 항목 | 팁 |
|---|---|
| **GPU 인스턴스 방치 금지** | 작업 스크립트 끝에 `sudo shutdown -h now` 또는 `gpu-off` alias 습관화 |
| **Spot 활용** | `p5.48xlarge`는 온디맨드 ~$98/hr → Spot ~$30~50/hr. 체크포인트를 S3에 자주 저장 |
| **편집박스 상시 Stop** | 작업 안 할 때는 Stop. EBS 30GB → ~$3/월만 청구 |
| **Budget 알람** | AWS 콘솔 → Billing → Budgets에서 월 한도 설정 및 초과 알람 등록 |
| **비용 확인** | AWS 콘솔 → Cost Explorer에서 인스턴스별 비용 추적 |

---

## 14. 자주 묻는 질문

### Q. KIRO vs Cursor, 뭘 써야 하나?

AWS 서비스를 많이 사용한다면 **KIRO**가 유리하다. IAM Identity Center, Bedrock, S3 연동이 설치 즉시 네이티브로 동작한다. Cursor도 MCP 서버를 추가하면 동등한 기능을 구현할 수 있지만 초기 설정 비용이 더 든다.

### Q. 편집박스에서 GPU 관련 코드를 편집해도 되나?

된다. CUDA 코드도 GPU 없이 **편집·문법 확인**은 가능하다. 실제 실행(CUDA 커널 실행)만 GPU가 필요하다. 편집박스는 코드를 쓰는 곳, GPU 박스는 실행해서 에러 확인하는 곳이다.

### Q. GPU 박스를 매번 새로 만들고 지워야 하나?

아니다. **한 번 만들고 Stop/Start**로 재사용한다. Terminate(삭제)는 프로젝트 완전 종료 시에만 한다. Stop 상태에서는 EBS 비용(~$3~5/월)만 청구된다.

### Q. 편집박스와 GPU 박스를 동시에 연결해야 하나?

아니다. GitHub을 통해 코드를 공유하므로, **편집박스에서 push → GPU 박스에서 pull**하면 된다. 두 인스턴스를 동시에 연결할 필요가 없다.

### Q. H100 단일 카드 인스턴스가 AWS에 있나?

없다. AWS p5 패밀리의 최소 단위는 H100 8장짜리 `p5.48xlarge`다. H100 1장만 쓰려면 `CUDA_VISIBLE_DEVICES=0`으로 1장만 지정해서 사용한다. H100 성능 벤치마크는 로컬 H100 서버에서 하고, AWS에서는 L40S(`g6e.xlarge`)로 기능 검증을 하는 것이 비용 효율적이다.

### Q. 로컬 H100 서버와 EC2의 코드 동기화는 어떻게?

GitHub이 중간 다리다. 편집박스에서 push → 로컬 H100 서버에서 `git pull`. 별도의 파일 전송(scp, rsync) 없이 GitHub을 통해 항상 동일한 코드를 유지한다. 데이터(대용량 파일)는 S3를 통해 전달한다.

### Q. SSM이 안 되면 어떻게 하나?

EC2 인스턴스에 IAM Instance Profile이 올바르게 부착되었는지 확인한다. `AmazonSSMManagedInstanceCore` 정책이 포함되어야 하고, EC2가 인터넷에 접근할 수 있는 VPC(기본 VPC 사용 권장)에 있어야 한다.

```bash
# SSM 상태 확인 (EC2 내부에서)
sudo systemctl status amazon-ssm-agent

# SSM Agent 재시작
sudo systemctl restart amazon-ssm-agent
```

### Q. KIRO에서 "Connect to Host"가 명령어 팔레트에 없다?

두 가지를 확인한다. ① `Open Remote - SSH` 확장(jeanp413, 292K 다운로드)이 설치되어 있지 않은 경우 — `Ctrl+Shift+X` → "Remote - SSH" 검색 → jeanp413 항목 설치. ② KIRO 계정 로그인 세션이 만료된 경우 — IAM Identity Center로 재로그인 후 MFA 인증. 둘 다 확인해야 연결이 된다. (§8.1, §8.2 참조)

### Q. .pem 파일은 어디에 저장해야 하나?

KIRO가 설치된 **로컬 PC의 `~/.ssh/` 폴더**에 저장한다. `~/.ssh/config`의 `IdentityFile`에 경로를 지정하면 SSH 접속 시 자동으로 사용된다.

```bash
mv ~/Downloads/your-key.pem ~/.ssh/your-key.pem
chmod 400 ~/.ssh/your-key.pem   # 권한 설정 필수
```

`.pem` 파일은 절대 GitHub에 올리면 안 된다. `.gitignore`에 `*.pem`을 추가해둔다.

### Q. GPU 인스턴스 시작 시 "vCPU limit of 0" 오류가 뜨면?

신규 AWS 계정은 GPU 인스턴스(G, P 패밀리)의 기본 vCPU 한도가 0이다. Service Quotas에서 증가 요청 후 승인(수 시간~1~2일)을 기다려야 한다. (§3.4 참조)

한도 승인 대기 중에는 `c7i.2xlarge`(편집박스)를 먼저 만들어 SSH 연결, GitHub 연동 등 기본 환경을 세팅해두는 것이 좋다.

### Q. 스토리지를 기본값(8GiB gp2)으로 그냥 써도 되나?

안 된다. 8GiB는 Ubuntu OS 설치만 해도 거의 꽉 찬다. CUDA, Python 패키지, 코드까지 설치하면 용량 부족으로 인스턴스가 정상 동작하지 않는다. **50GiB gp3**으로 변경하고 생성해야 한다. (그 이상도 가능)

### Q. 보안 그룹에서 SSH 인바운드(0.0.0.0/0)를 열어야 하나?

열면 안 된다. SSM ProxyCommand 방식은 EC2가 아웃바운드로 SSM 엔드포인트에 연결하는 구조라 인바운드 SSH 규칙이 불필요하다. "SSH 트래픽 허용 (0.0.0.0/0)" 체크를 해제하고, 아웃바운드만 전체 허용하면 된다.

---

## 커맨드 치트시트

```bash
# === 인스턴스 관리 ===
aws ec2 start-instances --instance-ids i-xxx     # 기동
aws ec2 stop-instances  --instance-ids i-xxx     # 중지
aws ec2 wait instance-running --instance-ids i-xxx  # 기동 완료까지 대기

# === SSH 접속 ===
ssh ec2-edit   # 편집박스 접속
ssh ec2-gpu    # GPU 박스 접속 (Running 상태일 때)

# === S3 파일 전송 ===
aws s3 sync data/      s3://dcl-datasets/...     # 업로드
aws s3 sync s3://dcl-datasets/... data/          # 다운로드
s5cmd sync data/ s3://dcl-datasets/...           # 대용량 고속 전송 (s5cmd 설치 시)

# === GPU 실행 (1장만 사용) ===
CUDA_VISIBLE_DEVICES=0 python run_channel_sim.py

# === 종료 후 즉시 인스턴스 중지 (GPU 박스에서) ===
sudo shutdown -h now
```

---

> **이전 매뉴얼**: [Manual 2: 브랜치 & 팀 Integration 가이드](Manual_2_Branch_Integration.md)
