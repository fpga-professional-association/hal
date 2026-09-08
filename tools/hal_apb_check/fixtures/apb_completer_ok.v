// APB4 completer with exactly one wait state -- the "correct" fixture.
//
// Gate-level Verilog against EXAMPLE_GATE_LIBRARY (plugins/gate_libraries/
// definitions/example_library.hgl, also shipped inside examples/uart.zip).
// FFR is Q' = R ? 0 : (D & CE) with an active-high reset R, so CE is tied to
// VCC everywhere and PRESETn is inverted into R.
//
// Behaviour, one step per rising PCLK edge:
//   access  = PSEL & PENABLE
//   w_q'    = access & ~w_q          (toggles while the ACCESS phase is held)
//   PREADY  = access & w_q           (low in the first ACCESS cycle, high in the second)
//   PSLVERR = PREADY & ERR_IN        (only ever asserted together with PREADY)
//   PRDATA0 = w_q ^ (PWRITE & PADDR0 & PWDATA0)
//
// Expected verdicts are in README.md next to this file.

module apb_completer_ok (
    PCLK,
    PRESETn,
    PSEL,
    PENABLE,
    PWRITE,
    PADDR_0,
    PWDATA_0,
    ERR_IN,
    PREADY,
    PRDATA_0,
    PSLVERR
);
    input PCLK;
    input PRESETn;
    input PSEL;
    input PENABLE;
    input PWRITE;
    input PADDR_0;
    input PWDATA_0;
    input ERR_IN;
    output PREADY;
    output PRDATA_0;
    output PSLVERR;

    wire PCLK;
    wire PRESETn;
    wire PSEL;
    wire PENABLE;
    wire PWRITE;
    wire PADDR_0;
    wire PWDATA_0;
    wire ERR_IN;
    wire PREADY;
    wire PRDATA_0;
    wire PSLVERR;

    wire vdd;
    wire rst;
    wire access;
    wire w_q;
    wire w_n;
    wire w_d;
    wire dat;

    VCC vcc_inst (
        .O(vdd)
    );
    INV rst_inv (
        .I(PRESETn),
        .O(rst)
    );
    AND2 access_and (
        .I0(PSEL),
        .I1(PENABLE),
        .O(access)
    );
    INV w_inv (
        .I(w_q),
        .O(w_n)
    );
    AND2 w_next_and (
        .I0(access),
        .I1(w_n),
        .O(w_d)
    );
    FFR w_reg (
        .C(PCLK),
        .CE(vdd),
        .D(w_d),
        .R(rst),
        .Q(w_q)
    );
    AND2 ready_and (
        .I0(access),
        .I1(w_q),
        .O(PREADY)
    );
    AND2 slverr_and (
        .I0(PREADY),
        .I1(ERR_IN),
        .O(PSLVERR)
    );
    AND3 dat_and (
        .I0(PWRITE),
        .I1(PADDR_0),
        .I2(PWDATA_0),
        .O(dat)
    );
    XOR rdata_xor (
        .I0(w_q),
        .I1(dat),
        .O(PRDATA_0)
    );
endmodule
