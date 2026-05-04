# Project Interview

Existing project — portal-ai (Portal.ai 패션 추천 AI 서버)

## Round 1: Ownership and Purpose

Question: 이 프로젝트의 현재 소유/목적 단계는?
Answer: 활발 개발 중인 제품. POC/초기 운영 단계이며 portal/app(Next.js)과 연동되어 추천 파이프라인 개선을 지속 중. 문서는 현재 구조 + 단기 로드맵을 반영한다.

## Round 2: Constraints and Non-Goals

Question: 문서화해야 할 제약조건/비목표는?
Answer: 성능/외부 의존 제약을 명시한다.
- Modal 콜드스타트(MODAL_EMBED_TIMEOUT 90s) 및 T4 GPU 로딩 버퍼
- Supabase HNSW 타임아웃 → 배치 chunk 25 + 자동 분할 재시도
- Langfuse v2 lock (서버 v2 이미지 호환, SDK <3.0)
- Supabase RPC `search_products_v5` 의존 (DB 스키마 변경 시 영향)
- Non-goals: 세션/인증(portal/app 책임), Vision 분석(GPT-4o-mini, portal/app 책임), 배치 추천

## Round 3: Documentation Priority

Question: 가장 정확하게 담아야 할 영역은?
Answer: 파이프라인 아키텍처 + 데이터 흐름.
- embed → search → diversify state machine
- Supabase RPC 경계와 dense(HNSW) + sparse(pgroonga) + RRF
- 다양성 캡 로직(브랜드/플랫폼 cap, tolerance→target_count)
- @observe 데코레이터 구조 (Langfuse trace SSOT)
