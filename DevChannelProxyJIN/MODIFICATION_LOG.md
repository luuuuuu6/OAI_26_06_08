# Modification Log

## G1B v1 — GPU IPC V5 Circular Buffer (2026-03-06)

### 개요
G1B v1: Socket-mode 1:1 대응 circular buffer over GPU IPC.
V1(ready-flag ping-pong)을 V5(timestamp-indexed circular buffer)로 교체.
DL/UL 대칭 채널 처리 추가.

### 추가된 파일

| 파일 | 위치 | 설명 |
|------|------|------|
| `gpu_ipc_v5.h` | `radio/rfsimulator/` | V5 SHM 레이아웃, 구조체, API 선언 |
| `gpu_ipc_v5.c` | `radio/rfsimulator/` | V5 구현: init, circ_write(gap-fill), circ_read, cleanup |
| `v1.py` | `G1B_MultiUE_MIMO_Channel_Proxy/` | GPUIpcV5Interface + timestamp polling + DL/UL 대칭 채널 |

### 수정된 파일

| 파일 | 변경 내용 |
|------|----------|
| `simulator.c` | `#ifdef USE_GPU_IPC_V5` 블록 추가 (include, struct, startServer, startClient, write, read, end, init). V5 최우선 cascade. |
| `CMakeLists.txt` | `gpu_ipc_v5.c` 추가, `USE_GPU_IPC_V5` 컴파일 정의 추가 |
| `launch_all.sh` | v0→RFSIM_GPU_IPC=1, v1→RFSIM_GPU_IPC_V5=1 분기 |
| `README.md` | v1 버전 이력, v0→v1 비교표, V5 수동 실행 안내 추가 |
| `실행_매뉴얼.txt` | v1 관련 정보 추가 |

### V5 설계

- **Circular buffer**: 460800 samples (10ms@30.72MHz), `offset = (ts * nbAnt) % cir_size`
- **Gap zero-fill**: Writer측에서 이전 write와 현재 write 사이 gap을 cudaMemset
- **Initial sync**: C측 reader가 첫 Proxy write의 timestamp로 nextRxTstamp 설정
- **Wait-with-timeout**: Reader가 30ms까지 1ms 간격으로 재시도, 이후 zero-fill
- **DL/UL 대칭 채널**: `--custom-channel` → DL+UL 모두 Sionna 채널, `-b` → 양방향 패스스루
- **환경변수**: `RFSIM_GPU_IPC_V5=1` (gNB, UE 모두)
- **SHM**: `/tmp/oai_gpu_ipc/gpu_ipc_shm` (4096 bytes, V5 layout)
- **기존 IPC와 공존**: V5가 최우선, 미설정 시 V4→V3→V2→V1 cascade

---

## v8 — Method A 복원 + Method C OAI-style 반복 폴링 (2026-03-04)

### 근본 원인
v8 초기 구현에서 gNB UL read에 5초 무한 블로킹(Method C)을 적용한 결과,
gNB RU 쓰레드가 UL read에서 정지 → DL write 불가 → UE에 SSB 미전달 → SSB 탐지 실패.

이후 0.5ms 1회 재시도 + zero-fill로 수정했으나, gNB가 UE의 UL 생산 속도와 무관하게
폭주하여 PBCH 디코딩 에러가 연속 발생하는 문제가 드러남.

### 변경 파일

#### `gpu_ipc_v2.h`
- `GPU_IPC_V2_RING_DEPTH`: 4 → 16 (v8 프록시와 일치)
- `GPU_IPC_V2_SHM_SIZE`: 4096 → 16384
- `gpu_ipc_v2_ctx_t`에 `int ul_ts_synced` 필드 추가
- SHM 레이아웃 주석 갱신 (오프셋 재계산)

#### `simulator.c` — `rfsimulator_read()` gNB UL RX 경로
- **Method A 복원**: 첫 UL 데이터 수신 시 ring의 stale 엔트리를 fast-forward 후
  `nextRxTstamp`을 마지막 ring TS로 1회 정렬. Backward jump guard 포함
  (`ts >= nextRxTstamp`일 때만 정렬, 역점프 방지).
- **Method C — OAI-style 반복 폴링**:
  - Phase 1 (sync 전): ring 비어있으면 `usleep(1ms)` + zero-fill. DL 파이프라인 유지.
    OAI socket 모드의 "no UE connected" 처리와 동일.
  - Phase 2 (sync 후): ring 비어있으면 `usleep(1ms) × max 30회` 반복 폴링.
    데이터 도착 시 즉시 소비, 30회 후에도 없으면 zero-fill.
    OAI socket 모드의 `epoll_wait(3ms) × N회` 패턴과 동일 원리.

#### `v8_multi_ue.py` — UL 진단 로깅 추가
- `[UL-DBG]`: 처음 5회 UL dequeue 시 ts/nsamps/nbAnt/data_size 출력
- `[UL-SRC]`: 각 UE의 ul_tx dequeue 후 non-zero/all-zero 판별 (처음 20/5회)
- `[UL-DST]`: bypass 경로에서 ul_rx에 복사된 데이터의 non-zero 확인 (처음 20회)

### OAI socket 모드와의 대응 관계

| OAI socket 모드 | GPU IPC Method C |
|-----------------|-----------------|
| `epoll_wait(3ms)` — UL 도착 대기 | `usleep(1ms)` + `gpu_ipc_v2_ul_read()` |
| `loops > 10` → 포기 (~30ms) | `ul_loops < 30` → 포기 (30ms) |
| TCP 커널 송신 버퍼 — 비동기 DL 전달 | `dl_tx_ring` (depth=16) + 독립 Proxy 프로세스 |
| gNB recv 버퍼 — UL 수신 | `ul_rx_ring` — Proxy가 UL 전달 |
| 4개 독립 커널 버퍼 (양방향 × 양단) | 4개 독립 ring buffer (dl_tx, ul_rx, dl_rx[k], ul_tx[k]) |

gNB가 UL 대기(Phase 2)로 블로킹되는 동안, `dl_tx_ring`에 사전 버퍼된 DL 데이터를
Proxy가 독립적으로 소비하여 UE에 전달. UE는 DL 처리 후 UL 생성 → Proxy가 `ul_rx_ring`에
기록 → gNB가 깨어나서 소비. TCP 소켓의 커널 비동기 전달과 동일한 파이프라인 구조.

### 설계 근거
| 방식 | DL 중단 | gNB 페이싱 | SSB 탐지 | PBCH |
|------|---------|-----------|----------|------|
| v8 초기 (5초 블로킹) | O (데드락) | UL 블로킹 (과도) | 실패 | - |
| v8 중간 (0.5ms 1회) | X | 없음 (폭주) | 성공 | 실패 |
| v7 (spin-wait 100µs) | X | 없음 (느림) | 성공 | 성공 |
| OAI socket (epoll 3ms×N) | X | UL 도착 속도에 동기 | 성공 | 성공 |
| **v8 최종 (1ms×30 폴링)** | X | UL 도착 속도에 동기 | 기대: 성공 | 기대: 성공 |
