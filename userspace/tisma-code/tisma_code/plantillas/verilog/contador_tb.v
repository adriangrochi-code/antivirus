// Banco de pruebas: comprueba el diseño y termina con «PASA» o «FALLA n». Úsalo con
// ingenieria accion=verilog archivo=contador.v opciones={"tb": "contador_tb.v"} (compila ambos).
`timescale 1ns/1ps
`include "contador.v"
module contador_tb;
    reg clk = 0, rst = 1, en = 0;
    wire [3:0] q;
    wire fin;
    integer errores = 0, i;
    contador #(.N(4)) dut (.clk(clk), .rst(rst), .en(en), .q(q), .fin(fin));
    always #5 clk = ~clk;
    initial begin
        @(posedge clk); #1 rst = 0; en = 1;
        for (i = 1; i <= 15; i = i + 1) begin
            @(posedge clk); #1;
            if (q !== i[3:0]) begin errores = errores + 1; $display("FALLA ciclo %0d: q=%0d", i, q); end
        end
        if (fin !== 1'b1) begin errores = errores + 1; $display("FALLA: fin no se activó en 15"); end
        @(posedge clk); #1;
        if (q !== 4'd0) begin errores = errores + 1; $display("FALLA: no volvió a 0"); end
        if (errores == 0) $display("PASA"); else $display("FALLA %0d", errores);
        $finish;
    end
endmodule
