import serial
import serial.tools.list_ports

def testar_porta(porta, baudrate=9600):
    try:
        with serial.Serial(porta, baudrate, timeout=1) as ser:
            return True
    except serial.SerialException as e:
        return False

def listar_e_testar_portas():
    portas = serial.tools.list_ports.comports()
    if not portas:
        print("Nenhuma porta serial encontrada.")
        return

    for porta in portas:
        nome = porta.device
        descricao = porta.description
        fabricante = porta.manufacturer or "Desconhecido"
        esta_livre = testar_porta(nome)

        status = "✅ LIVRE" if esta_livre else "❌ OCUPADA ou COM ERRO"
        print(f"{nome} - {descricao} - {fabricante} --> {status}")

if __name__ == "__main__":
    listar_e_testar_portas()
