# LabEdit

Primeiro corte de reacts no DaVinci Resolve. O LabEdit lê os arquivos da timeline ativa, sincroniza a câmera e a captura do OBS pelo áudio em comum, encurta silêncios nas duas faixas e monta tela dividida e closes. Nos closes, suaviza o acompanhamento do rosto e intercala diferentes níveis de zoom. Você revisa os trechos e envia a montagem como uma nova timeline no projeto.

## Instalar

O fluxo foi desenvolvido no Windows com DaVinci Resolve 21.0.4. Use Python 3.14 de 64 bits e FFmpeg com FFprobe no PATH ou o FFmpeg instalado pelo AutoSubs. O lançador do Resolve usa as APIs de script disponíveis nessa instalação.

1. Clone ou extraia o repositório para uma pasta fixa.
2. Execute **Instalar no Resolve.cmd**. O instalador cria um ambiente Python local, instala as dependências e adiciona **LabEdit** a **Workspace → Scripts**. A instalação aponta o menu para esta cópia do projeto.
3. Se o menu ainda estiver aberto com a lista antiga, reabra o Resolve.

## Usar

1. Coloque a webcam e a captura na timeline. As pastas `WEBCAM` e `TELACOMPLETA` permitem reconhecer automaticamente um par do OBS. A captura composta precisa incluir o mesmo microfone em uma faixa e o som isolado do vídeo reagido em outra.
2. Abra **Workspace → Scripts → LabEdit** e clique em **Gerar timeline**. Se os papéis dos arquivos forem ambíguos, escolha na lista.
3. Revise os trechos. Você pode alternar o enquadramento ou omitir um trecho.
4. Clique em **Enviar ao Resolve**. A ferramenta fecha após receber a confirmação. As mídias geradas vão para `renders/` por padrão. Mantenha essas mídias no lugar para que a timeline continue vinculada.

O sincronismo entre as gravações é automático. A correção fina de voz pode ser preenchida em milissegundos, com valores positivos atrasando o microfone. Calibrações específicas por gravação podem ser armazenadas em `.local/LabEdit-calibracoes.json` e ficam fora do Git.

Também é possível usar **Abrir LabEdit.cmd** no Windows ou chamar `.local\venv\Scripts\python.exe LabEdit.py --help` no terminal.

## Rastreamento de rosto

O detector YuNet roda localmente. O recorte é aplicado à mídia de close durante a preparação do envio, permitindo reproduzir a montagem sem executar o detector no Resolve. O projeto inclui o modelo e sua licença MIT. [OpenCV Zoo / YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet) · [Google AutoFlip](https://github.com/google-ai-edge/mediapipe/blob/master/docs/solutions/autoflip.md)
