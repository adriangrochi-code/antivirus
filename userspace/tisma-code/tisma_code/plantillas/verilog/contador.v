// Plantilla TISMA: contador síncrono de N bits con reinicio y habilitación.
module contador #(parameter N = 4) (
    input  wire         clk,
    input  wire         rst,
    input  wire         en,
    output reg  [N-1:0] q,
    output wire         fin      // 1 cuando q está al máximo
);
    assign fin = &q;
    always @(posedge clk) begin
        if (rst)     q <= {N{1'b0}};
        else if (en) q <= q + 1'b1;
    end
endmodule
