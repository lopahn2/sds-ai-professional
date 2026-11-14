import React from 'react';
import { createRoot } from 'react-dom/client';
import { NariaProvider } from '@naria/ui';
import '@naria/ui/styles.css';
import App from './App';

// naria 디자인 시스템의 토큰·서체를 화면 전체에 적용한다. 화면별 스타일은 naria.css가 이 토큰에 연결한다.
createRoot(document.getElementById('root')!).render(<React.StrictMode><NariaProvider theme="light" palette="modern" className="nds-root"><App /></NariaProvider></React.StrictMode>);
