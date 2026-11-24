# GPO Autofish

Macro de pesca automática para o **Grand Piece Online** (Roblox).
Funciona só lendo a tela e simulando o clique do mouse, sem mexer na memória do jogo.

## Como funciona

No minigame de pesca existe uma barra vertical azul-claro com:
- uma **zona escura** que você controla (segurar o clique faz ela subir, soltar faz ela descer);
- uma **linha branca**, que é o peixe.

O script captura só a área da barra várias vezes por segundo. Um **controlador PD** decide
quando segurar ou soltar o clique pra manter a zona em cima do peixe. Depois ele relança a vara sozinho.

## Instalação

Requer Python 3.10+ no Windows.

```bash
pip install -r requirements.txt
python autofish.py
```

## Uso

1. Equipe a vara e a isca e lance uma vez na mão até o minigame aparecer.
2. Aperte **F7**. Arraste a caixinha transparente sobre a barra e aperte **Enter** pra salvar.
3. Aperte **F8** pra testar a detecção. O log deve mostrar `(True, número, número)`.
4. Clique no Roblox e aperte **F6** pra ligar.

| Tecla | Ação |
|---|---|
| F6 | Liga/desliga |
| F7 | Definir área da barra |
| F8 | Testar detecção (salva `debug.png`) |
| F9 | Mostrar a cor RGB debaixo do mouse |
| F10 | Fechar |

## Ajustes

Pelo painel ou pelo `config.json` (criado na primeira execução):

- **Kp / Kd**: aumente o Kd se a zona ficar oscilando e diminua se ela reagir devagar.
- **Segurar p/ lançar**: força do lançamento.
- **Inverter**: se segurar o clique fizer a zona descer.
- **bar_rgb / zone_rgb / fish_white_min**: cores da barra, caso o jogo mude a interface.

O script só manda cliques quando a janela em foco é o Roblox.
